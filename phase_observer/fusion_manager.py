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
    """
    Récupère les seuils de confiance depuis la configuration.

    Ordre de priorité :
    1. cfg["fusion"]["scoring_thresholds"] (config_trade_scalping.json)
    2. cfg["scoring_thresholds"] (fallback ancien format)
    3. Valeurs par défaut hardcodées
    """
    cfg = cfg or {}

    # Cherche d'abord dans fusion.scoring_thresholds (nouveau format)
    fusion_cfg = cfg.get("fusion", {}) if isinstance(cfg, dict) else {}
    th = fusion_cfg.get("scoring_thresholds", {}) if isinstance(fusion_cfg, dict) else {}

    # Fallback sur ancien format (racine)
    if not th:
        th = cfg.get("scoring_thresholds", {}) if isinstance(cfg, dict) else {}

    return {
        "cautious": float(th.get("cautious", 0.55)),
        "moderate": float(th.get("moderate", 0.70)),
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

            # Métriques ticks (qualité des données)
            tick_count = fp_summary.get("tick_count", 0)
            coverage_s = fp_summary.get("coverage_s", 0.0)
            tick_rate = fp_summary.get("tick_rate", 0.0)
            # ✅ FIX (24 Nov 2025): Afficher les TICKS buy/sell au lieu des niveaux avec imbalance
            buy_volume = fp_summary.get("buy_volume", 0.0)  # Nombre de ticks buy
            sell_volume = fp_summary.get("sell_volume", 0.0)  # Nombre de ticks sell
            buy_pct = fp_summary.get("buy_pct", 50.0)  # % de ticks buy

            # Niveaux avec imbalance forte (pour info)
            imbalance_buy_levels = fp_summary.get("imbalance_buy", 0)
            imbalance_sell_levels = fp_summary.get("imbalance_sell", 0)

            self.log.info(f"│ Status     : {fp_status:<15} Score    : {fp_score:>6.2%}           │")
            self.log.info(f"│ Direction  : {fp_dir:<18} Delta    : {fp_delta:>8.1f}         │")
            self.log.info(f"│ POC Price  : {fp_poc if fp_poc else 'N/A':<20} Absorption: {'✅ YES' if fp_absorption else '❌ NO':<10}│")
            self.log.info(f"│                                                                     │")
            self.log.info(f"│ 📊 Qualité Données:                                                 │")
            self.log.info(f"│   • Ticks      : {tick_count:>6} ticks    Coverage: {coverage_s:>6.1f}s           │")
            self.log.info(f"│   • Tick Rate  : {tick_rate:>6.2f} ticks/s                               │")
            self.log.info(f"│                                                                     │")
            self.log.info(f"│ 📈 Répartition Ticks Acheteurs/Vendeurs:                            │")
            self.log.info(f"│   • Ticks Buy  : {int(buy_volume):>6} ticks ({buy_pct:>5.1f}%)                        │")
            self.log.info(f"│   • Ticks Sell : {int(sell_volume):>6} ticks ({100-buy_pct:>5.1f}%)                        │")
            self.log.info(f"│                                                                     │")
            self.log.info(f"│ 🎯 Niveaux avec Imbalance Forte (>70%):                            │")
            self.log.info(f"│   • Buy Levels : {imbalance_buy_levels:>3} niveaux  (pression acheteuse dominante)   │")
            self.log.info(f"│   • Sell Levels: {imbalance_sell_levels:>3} niveaux  (pression vendeuse dominante)   │")
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
            imbalance_mean = of_summary.get("imbalance_mean", imbalance)  # Alias
            cvd_slope = of_summary.get("cvd_slope", 0.0)
            bias = of_summary.get("bias", "NEUTRAL")

            # Volumes (calculés par OrderFlow V6 depuis tick_volume des barres)
            total_volume = of_summary.get("total_volume", 0.0)
            # OrderFlow calcule le delta et l'imbalance, on peut en déduire buy/sell
            # buy_volume ≈ (total * (1 + delta/total)) / 2
            # sell_volume ≈ (total * (1 - delta/total)) / 2
            if total_volume > 0 and of_delta != 0:
                buy_volume = (total_volume + of_delta) / 2.0
                sell_volume = (total_volume - of_delta) / 2.0
            else:
                # Fallback : utiliser imbalance_mean comme proxy du ratio buy/sell
                buy_volume = total_volume * imbalance_mean if total_volume > 0 else 0.0
                sell_volume = total_volume * (1.0 - imbalance_mean) if total_volume > 0 else 0.0

            buy_pct = (buy_volume / total_volume * 100) if total_volume > 0 else 50.0
            sell_pct = (sell_volume / total_volume * 100) if total_volume > 0 else 50.0

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
            self.log.info(f"│ 📈 Volume Distribution (tick_volume des barres M1):                 │")
            self.log.info(f"│   • Buy Volume : {buy_volume:>8.1f} ({buy_pct:>5.1f}%)                           │")
            self.log.info(f"│   • Sell Volume: {sell_volume:>8.1f} ({sell_pct:>5.1f}%)                           │")
            self.log.info(f"│   • Total      : {total_volume:>8.1f}                                       │")
            self.log.info(f"│                                                                     │")
            self.log.info(f"│ 📈 Orderflow Metrics:                                               │")
            self.log.info(f"│   • Imbalance  : {imbalance_mean:>6.3f}      (déséquilibre buy/sell)         │")
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

            # Section 3 : SYNTHÈSE GLOBALE (vision unifiée des 3 fonctions)
            self.log.info("┌─────────────────────────────────────────────────────────────────────┐")
            self.log.info("│ 🎯 SYNTHÈSE GLOBALE (Vision Unifiée)                                │")
            self.log.info("├─────────────────────────────────────────────────────────────────────┤")

            # Cohérence directionnelle (consensus)
            directions = []
            if n_of.get("dir", 0) != 0:
                directions.append(("OrderFlow", "BUY" if n_of.get("dir") > 0 else "SELL"))
            if n_fp.get("dir", 0) != 0:
                directions.append(("Footprint", "BUY" if n_fp.get("dir") > 0 else "SELL"))
            if n_tr.get("dir", 0) != 0 and tr_type not in ["none", "fusion_pretrigger"]:
                directions.append(("Trigger", "BUY" if n_tr.get("dir") > 0 else "SELL"))

            # Compter consensus
            buy_votes = sum(1 for _, d in directions if d == "BUY")
            sell_votes = sum(1 for _, d in directions if d == "SELL")
            total_votes = len(directions)

            if buy_votes == total_votes and total_votes > 0:
                consensus = f"✅ UNANIME BUY ({buy_votes}/{total_votes})"
            elif sell_votes == total_votes and total_votes > 0:
                consensus = f"✅ UNANIME SELL ({sell_votes}/{total_votes})"
            elif buy_votes > sell_votes:
                consensus = f"🟡 MAJORITAIRE BUY ({buy_votes}/{total_votes})"
            elif sell_votes > buy_votes:
                consensus = f"🟡 MAJORITAIRE SELL ({sell_votes}/{total_votes})"
            else:
                consensus = f"❌ CONFLIT ({buy_votes}B/{sell_votes}S)"

            self.log.info(f"│ Consensus : {consensus:<55}│")
            for func_name, dir_value in directions:
                emoji = "🟢" if dir_value == "BUY" else "🔴"
                self.log.info(f"│   • {func_name:<12}: {emoji} {dir_value:<10}                              │")

            self.log.info(f"│                                                                     │")

            # Delta combiné (OF + FP)
            delta_combined = abs(of_delta) + abs(fp_delta)
            delta_alignment = "✅ Alignés" if (of_delta * fp_delta) >= 0 else "❌ Divergents"
            self.log.info(f"│ Delta Combiné: {delta_combined:>8.1f}         {delta_alignment:<20}    │")
            self.log.info(f"│   • OrderFlow  : {of_delta:>8.1f}                                       │")
            self.log.info(f"│   • Footprint  : {fp_delta:>8.1f}                                       │")

            self.log.info(f"│                                                                     │")

            # Qualité des données (validation)
            data_quality_items = []
            if tick_count >= 100:
                data_quality_items.append("✅ Ticks suffisants")
            elif tick_count >= 50:
                data_quality_items.append("🟡 Ticks moyens")
            else:
                data_quality_items.append("❌ Ticks faibles")

            if fp_status == "VALID":
                data_quality_items.append("✅ FP valide")
            else:
                data_quality_items.append("❌ FP suspect")

            if of_status == "VALID":
                data_quality_items.append("✅ OF valide")
            else:
                data_quality_items.append("❌ OF suspect")

            self.log.info(f"│ Qualité: {' | '.join(data_quality_items):<56}│")
            self.log.info("└─────────────────────────────────────────────────────────────────────┘")

            # Section 4 : Décision finale
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
        adaptive_w = self._adaptive_weights(
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

    # -------------- 4) Composite Score (Primary Data) --------------
    def _calculate_composite_score(
        self, n_of: Dict, n_fp: Dict, n_tr: Dict, coherence: Dict, quality: Dict
    ) -> Dict[str, Any]:
        """
        Score composite basé sur les données PRIMORDIALES :
        - buy_volume / sell_volume (pression marché)
        - delta_total (force nette)
        - imbalance / buy_pct (domination)

        Pondération :
        - 40% : Pression (buy/sell volumes)
        - 20% : Delta (force nette)
        - 30% : Ratios (domination %)
        - 10% : Dynamique (momentum)

        Retourne : {
            "base_score": 0-1,
            "pression_score": 0-1,
            "delta_score": 0-1,
            "ratios_score": 0-1,
            "dynamique_score": 0-1,
        }
        """
        import numpy as np

        # ========== EXTRACTION DONNÉES PRIMORDIALES ==========

        # OrderFlow
        of_raw = n_of.get("raw", {})
        of_summary = of_raw.get("summary", {})
        if isinstance(of_summary, str):
            try:
                of_summary = ast.literal_eval(of_summary)
            except:
                of_summary = {}

        of_delta = _to_float(of_summary.get("delta_total"), 0.0)
        of_total_vol = _to_float(of_summary.get("volume_total"), 0.0)
        of_buy_ratio = _to_float(of_summary.get("buy_ratio"), 0.5)

        # Calcul buy/sell volumes : PRIORITÉ au buy_ratio (plus fiable que delta)
        if of_total_vol > 0:
            of_buy_vol = of_total_vol * of_buy_ratio
            of_sell_vol = of_total_vol * (1.0 - of_buy_ratio)
        else:
            of_buy_vol = 0.0
            of_sell_vol = 0.0

        of_imbalance = _to_float(of_summary.get("mean_imbalance"), 0.5)
        of_cvd_slope = _to_float(of_summary.get("cvd_slope"), 0.0)

        # Footprint M1
        fp_raw = n_fp.get("raw", {})
        fp_summary = fp_raw.get("summary", {})
        if isinstance(fp_summary, str):
            try:
                fp_summary = ast.literal_eval(fp_summary)
            except:
                fp_summary = {}

        fp_delta = _to_float(n_fp.get("delta_total"), 0.0)
        fp_buy_vol = _to_float(fp_summary.get("buy_volume"), 0.0)
        fp_sell_vol = _to_float(fp_summary.get("sell_volume"), 0.0)
        fp_total_vol = _to_float(fp_summary.get("total_volume", fp_buy_vol + fp_sell_vol), fp_buy_vol + fp_sell_vol)
        fp_tick_rate = _to_float(fp_summary.get("tick_rate"), 0.0)

        # ========== COMBINAISON DES SOURCES ==========

        # Volumes combinés (OF + FP)
        buy_vol_combined = of_buy_vol + fp_buy_vol
        sell_vol_combined = of_sell_vol + fp_sell_vol
        total_vol_combined = buy_vol_combined + sell_vol_combined

        # Delta combiné
        delta_combined = of_delta + fp_delta

        # Protection division par zéro
        if total_vol_combined < 1e-6:
            return {
                "base_score": 0.0,
                "pression_score": 0.0,
                "delta_score": 0.0,
                "ratios_score": 0.0,
                "dynamique_score": 0.0,
            }

        # ========== CALCUL SCORES PAR COMPOSANTE ==========

        # --- 1) PRESSION MARCHÉ (40%) ---
        buy_dominance = buy_vol_combined / total_vol_combined  # 0-1
        sell_dominance = sell_vol_combined / total_vol_combined  # 0-1

        # Asymétrie de pression (amplifiée au carré pour renforcer les écarts)
        # Ex: 60% buy → asymétrie 0.20 → 0.20² = 0.04 (trop faible)
        # Mieux : normalisation forte
        # Si buy_dominance > 0.5 : score = (buy_dom - 0.5) / 0.5
        # => 60% → (0.6-0.5)/0.5 = 0.2
        # => 70% → (0.7-0.5)/0.5 = 0.4
        # => 80% → (0.8-0.5)/0.5 = 0.6

        if buy_dominance > sell_dominance:
            pression_normalized = (buy_dominance - 0.5) / 0.5  # 0-1
        else:
            pression_normalized = (sell_dominance - 0.5) / 0.5  # 0-1

        pression_score = 0.40 * pression_normalized

        # --- 2) DELTA (20%) ---
        # Normalisation avec tanh (seuil 2000 pour XAUUSD)
        delta_normalized = np.tanh(abs(delta_combined) / 2000.0)  # 0-1

        delta_score = 0.20 * delta_normalized

        # --- 3) RATIOS (30%) ---
        # Imbalance deviation (OF)
        imbalance_deviation = abs(of_imbalance - 0.5) * 2  # 0-1

        # Buy percentage (FP)
        if fp_total_vol > 0:
            fp_buy_pct = fp_buy_vol / fp_total_vol
            buy_pct_deviation = abs(fp_buy_pct - 0.5) * 2  # 0-1
        else:
            buy_pct_deviation = 0.0

        # Moyenne des 2 ratios
        ratios_score = 0.30 * max(imbalance_deviation, buy_pct_deviation)

        # --- 4) DYNAMIQUE (10%) ---
        # CVD slope (momentum)
        cvd_normalized = np.tanh(abs(of_cvd_slope) / 2.0)  # 0-1

        # Tick rate (activité) - bonus si >40 ticks/s
        tick_rate_normalized = min(1.0, fp_tick_rate / 80.0) if fp_tick_rate > 0 else 0.0

        dynamique_score = 0.10 * (0.7 * cvd_normalized + 0.3 * tick_rate_normalized)

        # ========== SCORE DE BASE ==========
        base_score = pression_score + delta_score + ratios_score + dynamique_score

        return {
            "base_score": float(max(0.0, min(1.0, base_score))),
            "pression_score": float(pression_score),
            "delta_score": float(delta_score),
            "ratios_score": float(ratios_score),
            "dynamique_score": float(dynamique_score),
            "details": {
                "buy_vol_combined": float(buy_vol_combined),
                "sell_vol_combined": float(sell_vol_combined),
                "delta_combined": float(delta_combined),
                "buy_dominance": float(buy_dominance),
                "imbalance_deviation": float(imbalance_deviation),
                "cvd_normalized": float(cvd_normalized),
            }
        }

    # -------------- 5) Confidence Fusion System (Composite + Trigger Boost) --------------
    def _calculate_fused_confidence(
        self, n_of, n_fp, n_tr, coherence, quality, cfg, ctx, rules_eval
    ) -> float:
        """
        NOUVEAU SYSTÈME DE SCORING :

        1. Score Composite (90%) : Basé sur données primordiales (buy/sell volumes, delta, ratios)
        2. Filtre Qualité : tick_count, coverage_s, status
        3. BONUS Trigger (+15%) : Si pattern réel détecté (stacking/climax/absorption)
        4. Bonus/Malus Cohérence : Alignement 3/3, conflits

        Le trigger devient un AMPLIFICATEUR (pas un bloqueur).
        """

        # ========== 1. SCORE COMPOSITE (Données Primordiales) ==========
        composite = self._calculate_composite_score(n_of, n_fp, n_tr, coherence, quality)
        base_score = composite["base_score"]  # 0-1

        # ========== 2. FILTRE QUALITÉ ==========

        # Extraction métriques qualité
        fp_raw = n_fp.get("raw", {})
        fp_summary = fp_raw.get("summary", {})
        if isinstance(fp_summary, str):
            try:
                fp_summary = ast.literal_eval(fp_summary)
            except:
                fp_summary = {}

        tick_count = _to_float(fp_summary.get("tick_count"), 0.0)
        coverage_s = _to_float(fp_summary.get("coverage_s"), 0.0)
        status_of = n_of.get("status", "SUSPECT")
        status_fp = n_fp.get("status", "SUSPECT")

        # Multiplicateur qualité
        quality_multiplier = 1.0

        # Tick count minimum
        if tick_count < 50:
            quality_multiplier *= 0.3  # Pénalité sévère
        elif tick_count < 100:
            quality_multiplier *= 0.7

        # Coverage minimum
        if coverage_s < 10:
            quality_multiplier *= 0.4
        elif coverage_s < 20:
            quality_multiplier *= 0.8

        # Status validation
        if status_of != "VALID":
            quality_multiplier *= 0.7
        if status_fp != "VALID":
            quality_multiplier *= 0.7

        # Application filtre qualité
        base_score *= quality_multiplier

        # ========== 3. BONUS TRIGGER (Amplificateur) ==========

        trigger_boost = 0.0
        trigger_type = n_tr.get("type", "")
        trigger_conf = n_tr.get("score", 0.0)

        # Vérifie si trigger RÉEL (pas fusion_pretrigger)
        valid_patterns = [
            # Triggers existants
            "stacking", "climax", "absorption", "micro_stack", "micro_absorption",
            # Nouveaux triggers (Session 22 Nov 2025)
            "liquidation_clusters", "failed_breakout", "momentum_imbalance", "accumulation_zones"
        ]
        is_real_trigger = trigger_type in valid_patterns

        if is_real_trigger:
            # Bonus selon qualité du trigger
            # Triggers prioritaires : stacking, climax, liquidation_clusters, failed_breakout
            priority_triggers = [
                "stacking", "climax", "absorption",
                "liquidation_clusters", "failed_breakout", "momentum_imbalance"
            ]
            if trigger_conf >= 0.85 and trigger_type in priority_triggers:
                trigger_boost = 0.15  # +15% DIAMANT
            elif trigger_conf >= 0.75:
                trigger_boost = 0.12  # +12% PLATINE
            elif trigger_conf >= 0.65:
                trigger_boost = 0.08  # +8% OR
            else:
                trigger_boost = 0.05  # +5% ARGENT

            # Bonus volume exceptionnel (si disponible dans trigger metadata)
            tr_raw = n_tr.get("raw", {})
            tr_meta = tr_raw.get("meta", {}) if isinstance(tr_raw, dict) else {}
            snapshot_stats = tr_meta.get("snapshot_stats", {}) if isinstance(tr_meta, dict) else {}
            volume_zscore = _to_float(snapshot_stats.get("volume_zscore_max"), 0.0) if isinstance(snapshot_stats, dict) else 0.0

            if volume_zscore >= 2.5:
                trigger_boost += 0.03  # +3% événement exceptionnel

        # Application bonus trigger
        base_score += trigger_boost

        # ========== 4. BONUS/MALUS COHÉRENCE ==========

        # Bonus alignement 3/3 (si trigger présent)
        if is_real_trigger and rules_eval.get("aligned3"):
            base_score *= 1.08  # +8% consensus unanime

        # Malus conflits
        matrix = coherence.get("matrix", {})
        conflicts = sum(1 for v in matrix.values() if v == "conflict")
        if conflicts >= 2:
            base_score *= 0.85  # -15% conflit majeur
        elif conflicts == 1:
            base_score *= 0.92  # -8% conflit mineur

        # Bonus timing (si disponible)
        timing_bonus = rules_eval.get("timing_bonus", 0.0)
        if timing_bonus > 0:
            base_score += timing_bonus

        # ========== 5. NORMALISATION FINALE ==========
        final_score = max(0.0, min(0.99, float(base_score)))

        # Logging détaillé (si FUSION_PROBE actif)
        if FUSION_PROBE:
            _probe(
                self.log,
                f"[COMPOSITE_SCORE] base={composite['base_score']:.3f} "
                f"(pression={composite['pression_score']:.3f}, delta={composite['delta_score']:.3f}, "
                f"ratios={composite['ratios_score']:.3f}, dynamique={composite['dynamique_score']:.3f}) | "
                f"quality_mult={quality_multiplier:.3f} | trigger_boost={trigger_boost:.3f} | "
                f"final={final_score:.3f}"
            )

        return final_score

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
        ]  # l'ancre du trigger reste prioritaire; POC pris plus haut si None

        if mode == "HOLD":
            return {
                "action": "HOLD",
                "signal_type": "WAIT_CONFIRMATION",
                "direction": "NEUTRAL",
                "anchor_price": anchor_price,
            }

        # Récupération des seuils dynamiques depuis la configuration
        thresholds = _get_thresholds(cfg)
        high_threshold = thresholds.get("high", 0.80)
        moderate_threshold = thresholds.get("moderate", 0.70)
        cautious_threshold = thresholds.get("cautious", 0.55)

        if fused >= high_threshold and direction in ("BUY", "SELL"):
            return {
                "action": direction,
                "signal_type": f"HIGH_CONVICTION_{direction}",
                "direction": direction,
                "anchor_price": anchor_price,
            }
        if fused >= moderate_threshold and direction in ("BUY", "SELL"):
            return {
                "action": direction,
                "signal_type": f"MODERATE_{direction}",
                "direction": direction,
                "anchor_price": anchor_price,
            }
        if fused >= cautious_threshold and direction in ("BUY", "SELL"):
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
