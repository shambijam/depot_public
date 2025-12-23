# phase_observer/fusion_manager.py
from __future__ import annotations
from typing import Dict, Any, Optional, Tuple, List
import time, logging, ast
from phase_observer.vwap.config import get_regime_weights

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

    ✅ FIX (19 DEC 2025): cfg passé est DÉJÀ la section "fusion"
    (ligne 331 de fuse(): cfg = strategy_config["fusion"])

    Donc on lit directement cfg["scoring_thresholds"] et cfg["allow_conditional_entries"]
    """
    cfg = cfg or {}

    # cfg est DÉJÀ la section fusion, pas besoin de .get("fusion")
    th = cfg.get("scoring_thresholds", {}) if isinstance(cfg, dict) else {}

    # ✅ FIX (19 DEC 2025) : Aucun fallback - respecter config strictement
    return {
        "cautious": float(th["cautious"]),
        "moderate": float(th["moderate"]),
        "high": float(th["high"]),
        "conditional": float(th["conditional"]),
        "allow_conditional": bool(cfg["allow_conditional_entries"]),
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
    self, n_of: Dict[str, Any], n_fp: Dict[str, Any], n_vw: Dict[str, Any]
) -> str:
    """✅ MISE À JOUR (04 DEC 2025): Inclut VWAP dans hash cohérence (triggers supprimés)"""
    import hashlib

    base = "|".join([self._hashable(n_of), self._hashable(n_fp), self._hashable(n_vw)])
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
    self, n_of: Dict[str, Any], n_fp: Dict[str, Any], n_vw: Dict[str, Any]
) -> Dict[str, Any]:
    """✅ MISE À JOUR (04 DEC 2025): Triggers supprimés, VWAP utilisé"""
    available = {
        "orderflow": n_of["score"] > 0 or n_of["dir"] != 0,
        "footprint": True,  # on a toujours un status ; score heuristique si absent
        "vwap": n_vw["score"] > 0 or n_vw.get("bias", "neutral") != "neutral",
    }
    return {"available": available, "is_degraded": not available["vwap"]}


# ---------- Validation croisée ----------
def _cross_system_validation(
    self, n_of: Dict[str, Any], n_fp: Dict[str, Any], n_vw: Dict[str, Any]
) -> List[str]:
    """✅ MISE À JOUR (04 DEC 2025): Validation croisée avec VWAP (triggers supprimés)"""
    issues: List[str] = []
    try:
        if (
            abs(float(n_of.get("delta_total", 0))) > 100
            and abs(float(n_fp.get("delta_total", 0))) < 10
        ):
            issues.append("delta_mismatch_of_vs_fp")
    except Exception:
        pass

    # ✅ VWAP vs OrderFlow fort - alerte si conflit majeur
    if n_vw["dir"] != 0 and n_of["dir"] != 0 and n_of["score"] >= 0.7:
        if n_vw["dir"] * n_of["dir"] < 0:  # Directions opposées
            issues.append("vwap_vs_strong_OF_conflict")

    return issues


# ---------- Poids adaptatifs ----------
def _adaptive_weights(
    self,
    regime: Optional[str],
    volatility: Optional[str],
    session: Optional[str],
    cfg: Optional[Dict[str, Any]] = None,
    vwap_regime: Optional[str] = None
) -> Optional[Dict[str, float]]:
    """
    ✅ MAJ VWAP DYNAMIQUE (06 DEC 2025): Poids adaptatifs selon régime VWAP

    Arguments:
        regime: Régime PhaseObserver (trending_*, range_*, etc.) - LEGACY
        volatility: Volatilité ("high", "low", etc.) - LEGACY
        session: Session de trading ("london", "ny", "asia") - LEGACY
        cfg: Configuration avec poids de base
        vwap_regime: Régime VWAP ("TRENDING", "ACCUMULATION", "BALANCED", "TRANSITIONAL")

    Poids VWAP optimaux selon régime (doc MAJ_dynamique_VWAP.MD):
        - TRENDING: 50% (VWAP = indicateur principal, tendance institutionnelle)
        - BALANCED: 30% (VWAP = référence neutre, équilibre)
        - ACCUMULATION: 25% (VWAP = support/résistance, micro-structure dominante)
        - TRANSITIONAL: 20% (VWAP = peu fiable, chaos/compression)

    Note: vwap_regime PRIME sur les autres paramètres (regime, volatility, session)
    """
    # Valeurs par défaut None → pas d'override
    if not (regime or volatility or session or vwap_regime):
        return None

    # === PRIORITÉ 1 : VWAP REGIME (nouveau système dynamique - PRIORITAIRE) ===
    if vwap_regime:
        vr = str(vwap_regime).upper()

        # Lecture dynamique des poids depuis vwap_adaptive_config.json
        weights = get_regime_weights(vr)
        w_vw = weights["vwap"]
        w_of = weights["orderflow"]
        w_fp = weights["footprint"]
        w_mom = weights["momentum"]

        self.log.info(
            f"[ADAPTIVE_WEIGHTS] 📊 VWAP_REGIME={vr} → "
            f"VWAP={w_vw:.0%} OF={w_of:.0%} FP={w_fp:.0%} MOM={w_mom:.0%}"
        )

        # ✅ Retourner immédiatement (pas de normalisation car poids du JSON déjà normalisés)
        return {"orderflow": w_of, "footprint": w_fp, "vwap": w_vw, "momentum": w_mom}

    # === PRIORITÉ 2 : Ajustements LEGACY (si pas de VWAP regime) ===
    else:
        # ✅ FALLBACK (06 DEC 2025): Poids par défaut + ajustements legacy
        w_of = 0.30
        w_fp = 0.35
        w_vw = 0.25
        w_mom = 0.10  # ✅ Momentum fixe à 10%

        # Volatilité élevée → renforcer orderflow +10%, réduire VWAP -10%
        if (volatility or "").lower() in ("high", "elevated", "high_volatility"):
            w_of += 0.10
            w_vw -= 0.10

        # Trending → renforcer orderflow + VWAP ; Range → renforcer footprint (structure)
        if (regime or "").lower().startswith("trend"):
            w_of += 0.05  # OrderFlow important en trend
            w_vw += 0.03  # VWAP confirme trend
            w_fp -= 0.08  # Footprint moins pertinent
        elif (regime or "").lower().startswith("range"):
            w_fp += 0.10  # Structure footprint cruciale en range
            w_of -= 0.05
            w_vw -= 0.05

        # Session London/NY → orderflow + VWAP réactifs ; Asia → footprint/structure
        s = (session or "").lower()
        if "london" in s or "europe" in s or "ny" in s:
            w_of += 0.03  # Forte liquidité → orderflow fiable
            w_vw += 0.02  # VWAP institutionnel actif
            w_fp -= 0.05
        elif "asia" in s:
            w_fp += 0.05  # Sessions calmes → focus structure
            w_of -= 0.03
            w_vw -= 0.02

    # Normalise (incluant momentum)
    total = max(1e-9, w_of + w_fp + w_vw + w_mom)
    w_of, w_fp, w_vw, w_mom = w_of / total, w_fp / total, w_vw / total, w_mom / total

    return {"orderflow": w_of, "footprint": w_fp, "vwap": w_vw, "momentum": w_mom}


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
    Orchestration modulaire (OFv6 + Footprint + VWAP).

    ✅ MISE À JOUR (03 DEC 2025): Intégration module VWAP institutionnel
    - Triggers supprimés → Remplacés par VWAP Module
    - Pondération: OrderFlow 50% + Footprint 25% + VWAP 25%

    Sortie :
      {
        "ok": bool,
        "action": "BUY"|"SELL"|"HOLD",
        "signal_type": "HIGH_CONVICTION_*" | "MODERATE_*" | "CAUTIOUS_*" | "WAIT_CONFIRMATION" | "INSUFFICIENT_DATA",
        "direction": "BUY"|"SELL"|"NEUTRAL",
        "fused_confidence": float(0..1),
        "anchor_price": float|None,
        "rationale": str,
        "components": {"orderflow":..., "footprint":..., "vwap":...},
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
        n_of: Dict[str, Any],
        n_fp: Dict[str, Any],
        n_vw: Dict[str, Any],
        fused: float,
        decision: Dict[str, Any],
        weights: Dict[str, float],
        thresholds: Dict[str, float] = None,
    ):
        """
        📊 BILAN CONSOLIDÉ (Version Compacte - 27 Nov 2025)
        Architecture: OrderFlow (50%) + Footprint (30%) + Triggers (20%)

        === DÉSACTIVÉ (Session 28 Nov 2025) ===
        Remplacé par les bilans spécifiques des stratégies :
        - ScalpingStrategy._log_orderflow_consolidated_report()
        - LiquidityStrategy._log_liquidity_consolidated_report()
        """
        # ✅ MISE À JOUR (04 DEC 2025): Fonction désactivée et code mort supprimé
        return  # Désactivé - remplacé par logs spécifiques des stratégies

    # -------------- Public API --------------

    def fuse(
        self,
        orderflow: Dict[str, Any],
        footprint: Dict[str, Any],
        vwap: Optional[Dict[str, Any]] = None,
        strategy_config: Optional[Dict[str, Any]] = None,
        context: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """
        ✅ MISE À JOUR (04 DEC 2025): VWAP principal (triggers supprimés)
        - orderflow: Dict depuis OrderFlowV6.analyze()
        - footprint: Dict depuis FootprintM1.analyze()
        - vwap: Dict depuis VWAPAnalyzer.analyze() (score 0-1, bias, zone, régime, etc.)
        """
        cfg = (strategy_config or {}).get("fusion", {}) or {}
        ctx = context or {}

        # 🔍 DEBUG LOG ENTRÉE FUSION
        _probe(
            self.log,
            f"[FUSION_ENTREE] OrderFlow score={orderflow.get('score') if orderflow else 'N/A'} "
            f"status={orderflow.get('status') if orderflow else 'N/A'} | "
            f"Footprint status={footprint.get('status') if footprint else 'N/A'} | "
            f"VWAP score={vwap.get('score') if vwap else 'N/A'} bias={vwap.get('bias') if vwap else 'N/A'}"
        )

        # 1) Validation / qualité
        quality = self._validate_inputs(
            orderflow or {}, footprint or {}, vwap or {}
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
        n_vw = self._normalize_vwap(vwap or {})  # ✅ VWAP normalization

        # timestamps garantis
        n_of = self._ensure_timestamp(n_of, ctx)
        n_fp = self._ensure_timestamp(n_fp, ctx)
        n_vw = self._ensure_timestamp(n_vw, ctx)  # ✅ VWAP timestamp

        # validation croisée → warnings qualité
        x_issues = self._cross_system_validation(n_of, n_fp, n_vw)
        if x_issues:
            quality["warnings"].extend([f"csv:{w}" for w in x_issues])

        # 3) Cohérence (direction pondérée + matrice simple)
        _key = self._coh_key(n_of, n_fp, n_vw)  # ✅ Inclut VWAP
        coherence = self._coherence_cached(
            _key, lambda: self._analyze_coherence(n_of, n_fp, n_vw, ctx)
        )
        degraded = self._degraded_mode_decision(n_of, n_fp, n_vw)
        if degraded["is_degraded"]:
            quality["warnings"].append("degraded_mode_fallback")

        # 4) Règles métier scalping
        rules_eval = self._apply_business_rules(n_of, n_fp, n_vw, coherence, cfg, ctx)

        # 5) Confiance fusionnée (pondération + bonus cohérence − malus conflit)
        # ✅ MAJ (19 DEC 2025): OrderFlow + Footprint + VWAP + Momentum (poids adaptatifs)
        fused, vwap_contribution, momentum_contribution = self._calculate_fused_confidence(
            n_of, n_fp, n_vw, coherence, quality, cfg, ctx, rules_eval
        )

        # === DEBUG 1C: DUMP normalized inputs ===
        try:
            of_sum = (n_of.get("summary") or {}) if isinstance(n_of, dict) else {}
            fp_sum = (n_fp.get("summary") or {}) if isinstance(n_fp, dict) else {}
            vw_sum = (n_vw.get("summary") or {}) if isinstance(n_vw, dict) else {}
            _probe(
                self.log,
                "[FUSION/DUMP] OF(score=%.2f, |Δ|=%.1f, bias=%s) | FP(score=%.2f, tickrate=%.2f/s, cov=%.2fs) | VWAP(score=%.2f, bias=%s, zone=%s) | coherence=%.3f | fused=%.3f | quality=%s",
                float((n_of.get("score") if isinstance(n_of, dict) else 0.0) or 0.0),
                float(abs((of_sum.get("delta_total") or 0.0))),
                str(n_of.get("bias") if isinstance(n_of, dict) else None),
                float((n_fp.get("score") if isinstance(n_fp, dict) else 0.0) or 0.0),
                float((fp_sum.get("tick_rate") or 0.0)),
                float((fp_sum.get("coverage_s") or 0.0)),
                float((n_vw.get("score") if isinstance(n_vw, dict) else 0.0) or 0.0),
                str(n_vw.get("bias") if isinstance(n_vw, dict) else None),
                str(n_vw.get("zone") if isinstance(n_vw, dict) else None),
                float((coherence or 0.0)),
                float((fused or 0.0)),
                str(quality),
            )
        except Exception:
            pass

        # 7) Génération décision (catégories + action BUY/SELL/HOLD)
        decision = self._final_decision("AUTO", fused, n_vw, coherence, cfg, ctx)

        # 9) Rationale
        rationale = self._rationale(decision, n_of, n_fp, n_vw, coherence, rules_eval)
        # metrics update
        self._update_metrics(
            dt=_now_ts() - t0, decision=decision["action"], fused=fused
        )

        # consensus texte BUY/SELL/TIE
        maj = coherence["majority"]
        maj_str = "BUY" if maj > 0 else ("SELL" if maj < 0 else "TIE")

        # ok=True seulement si action != HOLD ET fused >= seuil minimum (depuis config)
        # ✅ FIX (19 DEC 2025): Respecter allow_conditional_entries dynamiquement
        thresholds = _get_thresholds(cfg)
        if thresholds["allow_conditional"]:
            min_threshold = thresholds["conditional"]
        else:
            min_threshold = thresholds["cautious"]
        is_actionable = decision["action"] != "HOLD" and fused >= min_threshold

        # 📊 BILAN CONSOLIDÉ : Rapport unifié des 3 fonctions
        # Récupérer les poids utilisés pour la fusion (AVEC cfg pour lire poids de base)
        # ✅ MAJ (06 DEC 2025): Passer vwap_regime pour poids adaptatifs dynamiques
        vwap_regime = n_vw.get("regime")  # "TRENDING", "ACCUMULATION", "BALANCED", "TRANSITIONAL"
        adaptive_w = self._adaptive_weights(
            ctx.get("regime"), ctx.get("volatility"), ctx.get("session"), cfg, vwap_regime
        )
        if adaptive_w:
            weights_used = adaptive_w
        else:
            # ✅ FALLBACK (06 DEC 2025): Poids par défaut si système adaptatif échoue
            weights_used = {
                "orderflow": 0.30,
                "footprint": 0.35,
                "vwap": 0.35,
                "trigger": 0.0,  # DEPRECATED
            }

        # Appeler le rapport consolidé (actif seulement si FUSION_PROBE=1)
        if FUSION_PROBE:
            asset_name = ctx.get("asset", "UNKNOWN")
            # ✅ Récupérer les seuils configurés pour affichage dynamique
            configured_thresholds = _get_thresholds(cfg)
            self._log_consolidated_report(
                asset=asset_name,
                n_of=n_of,
                n_fp=n_fp,
                n_vw=n_vw,
                fused=fused,
                decision=decision,
                weights=weights_used,
                thresholds=configured_thresholds,
            )

        return {
            "ok": is_actionable,
            "action": decision["action"],
            "signal_type": decision["signal_type"],
            "direction": decision["direction"],
            "fused_confidence": round(float(fused), 3),
            "vwap_contribution": round(float(vwap_contribution), 3),  # ✅ Contribution VWAP
            "momentum_contribution": round(float(momentum_contribution), 3),  # ✅ Contribution Momentum (19 DEC 2025)
            "trigger_boost": 0.0,  # DEPRECATED (gardé pour rétrocompat)
            "anchor_price": decision["anchor_price"],
            "rationale": rationale,
            "components": {
                "orderflow": n_of,
                "footprint": n_fp,
                "vwap": n_vw,  # ✅ VWAP principal
            },
            "consensus": {
                "maj": maj_str,
                "agreement": round(coherence["agreement"], 3),
                "votes": coherence["votes"],
            },
            "quality": quality,
        }

    # -------------- 1) Input Validator --------------
    def _validate_inputs(
        self,
        of: Dict[str, Any],
        fp: Dict[str, Any],
        vw: Dict[str, Any]
    ) -> Dict[str, Any]:
        """
        ✅ MISE À JOUR (04 DEC 2025): VWAP validation (triggers supprimés)
        """
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
        # VWAP optionnel mais recommandé
        if not vw or (vw.get("score") is None and vw.get("bias") is None):
            warnings.append("vwap.missing_data")

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

        # ✅ FIX (05 DEC 2025): SUPPRESSION PÉNALITÉ STATUS ARBITRAIRE
        # Le score OrderFlow V6 est DÉJÀ calculé avec qualité intégrée (delta + volume + imbalance)
        # Pénaliser à nouveau sur status="WEAK" est une double pénalité injustifiée
        # Ancien code: if status != "VALID": score01 *= 0.6
        # Résultat: score=30 devient 0.18 au lieu de 0.30 → bot bloqué artificiellement

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

        # ✅ FIX (05 DEC 2025): SUPPRESSION PÉNALITÉ STATUS ARBITRAIRE
        # Même raisonnement que OrderFlow - le score Footprint intègre déjà la qualité
        # Ancien code: if status != "VALID": score01 *= 0.6

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

    def _normalize_vwap(self, vw: Dict[str, Any]) -> Dict[str, Any]:
        """
        ✅ NOUVEAU (03 DEC 2025): Normalise résultat VWAPAnalyzer

        Input (depuis VWAPAnalyzer.analyze()):
          {
            "score": 0.0-1.0,  # Déjà normalisé
            "status": "VALID"|"SUSPECT"|"INVALID",
            "bias": "BUY"|"SELL"|"NEUTRAL",
            "vwap_value": float,
            "distance_pips": float,
            "slope": float,
            "zone": "NEUTRAL"|"STRONG"|"EXTREME",
            "regime": "ACCUMULATION"|"TRENDING"|"BALANCED"|"TRANSITIONAL",
            "summary": {...}
          }

        Output normalisé:
          {
            "score": 0.0-1.0,
            "status": str,
            "dir": -1|0|1,
            "bias": str,
            "zone": str,
            "regime": str,
            "vwap_value": float,
            "distance_pips": float,
            "slope": float,
            "summary": dict,
            "raw": dict
          }
        """
        if not vw:
            return {
                "score": 0.0,
                "status": "INVALID",
                "dir": 0,
                "bias": "NEUTRAL",
                "zone": "NEUTRAL",
                "regime": "BALANCED",
                "vwap_value": 0.0,
                "distance_pips": 0.0,
                "slope": 0.0,
                "summary": {},
                "raw": {}
            }

        # Score déjà normalisé 0-1
        score = max(0.0, min(1.0, _to_float(vw.get("score"), 0.0)))

        # Status
        status = str(vw.get("status", "SUSPECT")).upper()

        # Bias → direction
        bias = str(vw.get("bias", "NEUTRAL")).upper()
        dir_int = 1 if bias == "BUY" else (-1 if bias == "SELL" else 0)

        # Métriques VWAP
        zone = str(vw.get("zone", "NEUTRAL"))
        # ✅ FIX (09 DEC 2025): Lire regime depuis vw["vwap"]["regime"] si structure to_dict()
        # sinon fallback sur vw["regime"] (si dict direct depuis analyzer)
        vwap_sub = vw.get("vwap", {})
        if isinstance(vwap_sub, dict) and "regime" in vwap_sub:
            regime = str(vwap_sub["regime"])
        else:
            regime = str(vw.get("regime", "BALANCED"))
        vwap_value = _to_float(vw.get("vwap_value"), 0.0)
        distance_pips = _to_float(vw.get("distance_pips"), 0.0)
        slope = _to_float(vw.get("slope"), 0.0)

        # Summary
        summary = vw.get("summary") or {}
        if isinstance(summary, str):
            try:
                summary = ast.literal_eval(summary)
            except Exception:
                summary = {}

        # ✅ FIX (05 DEC 2025): SUPPRESSION PÉNALITÉ STATUS ARBITRAIRE
        # Même logique - le score VWAP est déjà calculé avec qualité intégrée
        # Ancien code: if status != "VALID": score *= 0.8

        return {
            "score": score,
            "status": status,
            "dir": dir_int,
            "bias": bias,
            "zone": zone,
            "regime": regime,
            "vwap_value": vwap_value,
            "distance_pips": distance_pips,
            "slope": slope,
            "summary": summary,
            "raw": vw,
        }

    # -------------- 2) Coherence Analyzer --------------
    def _analyze_coherence(
        self,
        n_of: Dict[str, Any],
        n_fp: Dict[str, Any],
        n_vw: Dict[str, Any],
        ctx: Dict[str, Any],
    ) -> Dict[str, Any]:
        """✅ MISE À JOUR (04 DEC 2025): Triggers supprimés, VWAP principal"""
        votes = []
        if n_of["dir"] != 0:
            votes.append(("orderflow", n_of["dir"], n_of["score"]))
        if n_fp["dir"] != 0:
            votes.append(("footprint", n_fp["dir"], n_fp["score"]))

        # ✅ MAJ (06 DEC 2025): VWAP vote - boost supprimé, poids adaptatifs gérés par _adaptive_weights()
        if n_vw["dir"] != 0:
            # Score VWAP brut (pas de boost - les poids adaptatifs sont dans _adaptive_weights)
            vwap_weight = n_vw["score"]
            votes.append(("vwap", n_vw["dir"], vwap_weight))

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
        lead = {"orderflow_age_s": None, "footprint_age_s": None, "vwap_age_s": None}
        for k, n in (
            ("orderflow_age_s", n_of),
            ("footprint_age_s", n_fp),
            ("vwap_age_s", n_vw),  # ✅ FIX (03 DEC 2025): VWAP timestamp tracking
        ):
            ts = n.get("ts")
            if ts is not None:
                try:
                    lead[k] = max(0.0, float(now_ts) - float(ts))
                except Exception:
                    lead[k] = None

        # ✅ FIX (03 DEC 2025): Matrice de cohérence mise à jour avec VWAP
        matrix = {
            "of_vs_fp": (
                "aligned"
                if n_of["dir"] == n_fp["dir"]
                else "conflict" if (n_of["dir"] * n_fp["dir"] < 0) else "neutral"
            ),
            "of_vs_vwap": (
                "aligned"
                if n_of["dir"] == n_vw["dir"]
                else "conflict" if (n_of["dir"] * n_vw["dir"] < 0) else "neutral"
            ),
            "fp_vs_vwap": (
                "aligned"
                if n_fp["dir"] == n_vw["dir"]
                else "conflict" if (n_fp["dir"] * n_vw["dir"] < 0) else "neutral"
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
        self, n_of, n_fp, n_vw, coherence, cfg, ctx
    ) -> Dict[str, Any]:
        """
        ✅ MISE À JOUR (04 DEC 2025): Triggers supprimés, règles simplifiées

        Règles actives :
        - WEAK FOOTPRINT : Score FP < seuil → note
        - TRIPLE CONFIRMATION : OF + FP + VWAP tous alignés → bonus

        Règles SUPPRIMÉES (dépendaient de triggers) :
        - NEVER AGAINST STRONG ORDERFLOW (comparait trigger vs OF)
        - ABSORPTION VETO (comparait trigger vs FP)
        - TIMING BONUS (age du trigger)
        """
        rules = cfg.get("regles_metier", {}) or {}
        triple_bonus = bool(rules.get("triple_confirmation_bonus", True))
        weak_fp_penalty_th = _to_float(rules.get("weak_footprint_score_th", 0.60), 0.60)

        allow = True
        notes: List[str] = []

        # "WEAK FOOTPRINT PENALTY"
        weak_fp = n_fp["score"] < weak_fp_penalty_th
        if weak_fp:
            notes.append("WEAK_FOOTPRINT")

        # "TRIPLE CONFIRMATION BONUS" (cohérence 3/3 : OF + FP + VWAP)
        matrix = coherence.get("matrix", {})
        aligned3 = (
            matrix.get("of_vs_fp") == "aligned"
            and matrix.get("of_vs_vwap") == "aligned"
            and matrix.get("fp_vs_vwap") == "aligned"
        )

        # Timing bonus supprimé (dépendait de trigger_age)
        timing_bonus = 0.0

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
        self, n_of: Dict, n_fp: Dict, coherence: Dict, quality: Dict
    ) -> Dict[str, Any]:
        """
        ✅ MISE À JOUR (04 DEC 2025): Triggers supprimés complètement

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

    # -------------- 5) Confidence Fusion System (Simple Average + Trigger Boost) --------------
    def _calculate_fused_confidence(
        self, n_of, n_fp, n_vw, coherence, quality, cfg, ctx, rules_eval
    ) -> Tuple[float, float]:
        """
        ✅ MISE À JOUR (06 DEC 2025): POIDS ADAPTATIFS VWAP DYNAMIQUES

        NOUVELLE FORMULE DE FUSION:
        - Poids adaptatifs selon régime VWAP (TRENDING/BALANCED/ACCUMULATION/TRANSITIONAL)
        - TRENDING:      VWAP 50% / OF 30% / FP 20%
        - BALANCED:      VWAP 30% / OF 35% / FP 35%
        - ACCUMULATION:  VWAP 25% / OF 35% / FP 40%
        - TRANSITIONAL:  VWAP 20% / OF 40% / FP 40%

        1. Pondération adaptative (via _adaptive_weights)
        2. Bonus/Malus Cohérence : Alignement 3/3, conflits
        3. TRIGGERS SUPPRIMÉS

        Returns:
            (final_score, vwap_contribution)
        """

        # ========== 1. SCORES NORMALISÉS (0-1) ==========
        of_score = _to_float(n_of.get("score"), 0.0)
        fp_score = _to_float(n_fp.get("score"), 0.0)
        vw_score = _to_float(n_vw.get("score"), 0.0)

        # ✅ NOUVEAU (19 DEC 2025): Récupération score momentum
        momentum_result = ctx.get("momentum_result", {})
        momentum_score_raw = _to_float(momentum_result.get("total_score", 0.0), 0.0)  # 0-100
        momentum_score = momentum_score_raw / 100.0  # Normaliser à 0-1
        momentum_quality = momentum_result.get("quality", "N/A")
        momentum_direction = momentum_result.get("direction", "NEUTRAL")

        # ========== 2. POIDS ADAPTATIFS (système dynamique VWAP + MOMENTUM) ==========
        # ✅ MAJ (19 DEC 2025): Poids adaptatifs incluent momentum
        vwap_regime = n_vw.get("regime")  # "TRENDING", "ACCUMULATION", "BALANCED", "TRANSITIONAL"
        adaptive_w = self._adaptive_weights(
            ctx.get("regime"), ctx.get("volatility"), ctx.get("session"), cfg, vwap_regime
        )

        if adaptive_w:
            # Poids adaptatifs calculés dynamiquement (incluent momentum)
            w_of = adaptive_w.get("orderflow", 0.30)
            w_fp = adaptive_w.get("footprint", 0.35)
            w_vw = adaptive_w.get("vwap", 0.25)
            w_mom = adaptive_w.get("momentum", 0.10)  # ✅ NOUVEAU: Poids momentum
        else:
            # Fallback: poids par défaut (si aucune adaptation possible)
            w_of = 0.30
            w_fp = 0.35
            w_vw = 0.25
            w_mom = 0.10

        # ========== 3. FUSION PONDÉRÉE AVEC POIDS ADAPTATIFS (4 COMPOSANTS) ==========
        weighted_score = (of_score * w_of) + (fp_score * w_fp) + (vw_score * w_vw) + (momentum_score * w_mom)

        # Contributions pour tracking
        vwap_contribution = vw_score * w_vw
        momentum_contribution = momentum_score * w_mom

        # ========== 2. MÉTRIQUES QUALITÉ (Capturées mais Sans Pénalité) ==========

        # Extraction métriques qualité
        fp_raw = n_fp.get("raw", {})
        fp_summary = fp_raw.get("summary", {})
        if isinstance(fp_summary, str):
            try:
                import json
                fp_summary = json.loads(fp_summary)
            except:
                fp_summary = {}

        # ✅ SUPPRIMÉ (25 Nov 2025): Pénalités qualité arbitraires
        # Raison: Aucune validation empirique. On collecte les données SANS filtrage,
        # puis on analysera si tick_count/coverage_s/status impactent réellement le win rate.
        #
        # Ancien code (pénalités inventées):
        # - if tick_count < 50: score *= 0.3   → Pourquoi 50 ? Pourquoi 0.3 ?
        # - if coverage_s < 10: score *= 0.4   → Pourquoi 10s ? Pourquoi 0.4 ?
        # - if status != "VALID": score *= 0.7 → Pourquoi 0.7 ?
        #
        # Décision: Valider avec données réelles (après 100+ trades) si ces métriques
        # ont vraiment un impact. Si oui, ajuster. Si non, laisser sans pénalité.

        # Métriques capturées pour analyse (mais pas de pénalité appliquée)
        tick_count = _to_float(fp_summary.get("tick_count"), 0.0)
        coverage_s = _to_float(fp_summary.get("coverage_s"), 0.0)
        status_of = n_of.get("status", "SUSPECT")
        status_fp = n_fp.get("status", "SUSPECT")

        # ========== 3. BONUS/MALUS COHÉRENCE ==========
        # Malus conflits (si 2+ conflits entre composants)
        matrix = coherence.get("matrix", {})
        conflicts = sum(1 for v in matrix.values() if v == "conflict")
        if conflicts >= 2:
            weighted_score *= 0.85  # -15% conflit majeur
        elif conflicts == 1:
            weighted_score *= 0.92  # -8% conflit mineur

        # Bonus alignement 3/3 (tous alignés)
        alignments = sum(1 for v in matrix.values() if v == "aligned")
        if alignments >= 3:
            weighted_score += 0.05  # +5% pour alignement parfait

        # Bonus timing (si disponible)
        timing_bonus = rules_eval.get("timing_bonus", 0.0)
        if timing_bonus > 0:
            weighted_score += timing_bonus

        # ========== 4. NORMALISATION FINALE ==========
        final_score = max(0.0, min(0.99, float(weighted_score)))

        # Logging détaillé (si FUSION_PROBE actif)
        if FUSION_PROBE:
            status_vw = n_vw.get("status", "SUSPECT")
            _probe(
                self.log,
                f"[MOMENTUM_FUSION] 📊 OF={of_score:.3f}({w_of*100:.0f}%) + FP={fp_score:.3f}({w_fp*100:.0f}%) + VWAP={vw_score:.3f}({w_vw*100:.0f}%) + MOM={momentum_score:.3f}({w_mom*100:.0f}%) = {weighted_score:.3f} | "
                f"Mom: {momentum_direction} {momentum_quality} ({momentum_score_raw:.1f}/100) | "
                f"ticks={tick_count} cov={coverage_s}s | "
                f"status: OF={status_of} FP={status_fp} VWAP={status_vw} | "
                f"conflicts={conflicts} alignments={alignments} | "
                f"final={final_score:.3f} vwap_contrib={vwap_contribution:.3f} mom_contrib={momentum_contribution:.3f}"
            )

        # Retourner (score final, vwap_contribution, momentum_contribution)
        return (final_score, vwap_contribution, momentum_contribution)

    # -------------- 5b) Range Reversal Logic --------------
    def _apply_range_reversal_logic(
        self, direction: str, ctx: Dict[str, Any], n_vw: Dict[str, Any]
    ) -> Tuple[str, bool]:
        """
        ✅ CORRIGÉ (15 DEC 2025): Logique de retournement intelligente
        
        Règles CORRIGÉES :
        - Upper tercile (66%+) + signal BUY → VÉRIFIER avant d'inverser
        - Lower tercile (33%-) + signal SELL → VÉRIFIER avant d'inverser
        - Prendre en compte les confirmations des autres indicateurs
        """
        if direction == "NEUTRAL":
            return (direction, False)

        # Vérifier si on est en régime RANGE
        vwap_regime = n_vw.get("regime", "").upper()
        phase_regime = ctx.get("phase_observer_regime", "").lower()

        # 🔍 DEBUG amélioré
        range_pos = ctx.get("range_pos_pct", 0.5)
        _probe(
            self.log,
            f"[RANGE_CHECK_DEBUG] direction={direction} | pos={range_pos:.0%} "
            f"| upper={ctx.get('in_upper_tercile')} | lower={ctx.get('in_lower_tercile')} "
            f"| vwap={vwap_regime} | phase={phase_regime}"
        )

        # Détection régime RANGE
        is_range_regime = (
            vwap_regime == "BALANCED" or
            "range" in phase_regime or
            "compression" in phase_regime or
            "sideways" in phase_regime
        )

        if not is_range_regime:
            _probe(self.log, "[RANGE_REVERSAL] ❌ Pas en régime range - pas d'inversion")
            return (direction, False)

        # Récupérer les signaux de confirmation
        orderflow_bias = ctx.get("orderflow_bias", "NEUTRAL")
        absorption_bias = ctx.get("absorption_bias", "NEUTRAL")
        momentum_m1 = ctx.get("momentum_m1", "NEUTRAL")
        
        # Récupérer les données footprint
        footprint_data = ctx.get("footprint_data", {})
        buy_ratio = footprint_data.get("buy_ratio", 0.5)
        sell_ratio = footprint_data.get("sell_ratio", 0.5)
        
        # Récupérer les données OrderFlow
        orderflow_data = ctx.get("orderflow_data", {})
        delta_total = orderflow_data.get("delta_total", 0)
        
        # Seuils intelligents (configurables)
        EXTREME_UPPER = 0.85  # 85% pour considérer comme extrême
        EXTREME_LOWER = 0.15  # 15% pour considérer comme extrême
        CONFIRMATION_THRESHOLD = 0.6  # 60% de confiance requise

        in_upper = ctx.get("in_upper_tercile", False)
        in_lower = ctx.get("in_lower_tercile", False)

        # Logique de décision améliorée
        reversed_direction = direction
        was_reversed = False
        reversal_reason = ""

        # CAS 1: Haut du range + signal BUY
        if in_upper and direction == "BUY":
            # Vérifier les confirmations avant d'inverser
            confirmations = 0
            total_checks = 4
            
            # Check 1: Ratio d'achat faible (< 45%)
            if buy_ratio < 0.45:
                confirmations += 1
                reversal_reason += "Ratio achat faible, "
            
            # Check 2: OrderFlow bearish ou neutre
            if orderflow_bias in ["BEARISH", "NEUTRAL"]:
                confirmations += 1
                reversal_reason += f"OrderFlow {orderflow_bias}, "
            
            # Check 3: Momentum M1 bearish
            if momentum_m1 == "BEARISH":
                confirmations += 1
                reversal_reason += "Momentum bearish, "
            
            # Check 4: Position extrême (> 85%)
            if range_pos > EXTREME_UPPER:
                confirmations += 1
                reversal_reason += f"Position extrême ({range_pos:.0%}), "
            
            # Décision
            confidence = confirmations / total_checks
            if confidence >= CONFIRMATION_THRESHOLD:
                reversed_direction = "SELL"
                was_reversed = True
                _probe(
                    self.log,
                    f"[RANGE_REVERSAL] 🔄 INVERSION BUY→SELL | Confiance: {confidence:.0%} "
                    f"| Raison: {reversal_reason} | Pos: {range_pos:.0%}"
                )
            else:
                _probe(
                    self.log,
                    f"[RANGE_REVERSAL] ✅ MAINTIEN BUY | Confiance insuffisante: {confidence:.0%} "
                    f"| Ratio achat: {buy_ratio:.0%} | OrderFlow: {orderflow_bias}"
                )

        # CAS 2: Bas du range + signal SELL
        elif in_lower and direction == "SELL":
            confirmations = 0
            total_checks = 4
            
            # Check 1: Ratio d'achat fort (> 55%)
            if buy_ratio > 0.55:
                confirmations += 1
                reversal_reason += "Ratio achat fort, "
            
            # Check 2: OrderFlow bullish ou neutre
            if orderflow_bias in ["BULLISH", "NEUTRAL"]:
                confirmations += 1
                reversal_reason += f"OrderFlow {orderflow_bias}, "
            
            # Check 3: Momentum M1 bullish
            if momentum_m1 == "BULLISH":
                confirmations += 1
                reversal_reason += "Momentum bullish, "
            
            # Check 4: Position extrême (< 15%)
            if range_pos < EXTREME_LOWER:
                confirmations += 1
                reversal_reason += f"Position extrême ({range_pos:.0%}), "
            
            # Décision
            confidence = confirmations / total_checks
            if confidence >= CONFIRMATION_THRESHOLD:
                reversed_direction = "BUY"
                was_reversed = True
                _probe(
                    self.log,
                    f"[RANGE_REVERSAL] 🔄 INVERSION SELL→BUY | Confiance: {confidence:.0%} "
                    f"| Raison: {reversal_reason} | Pos: {range_pos:.0%}"
                )
            else:
                _probe(
                    self.log,
                    f"[RANGE_REVERSAL] ✅ MAINTIEN SELL | Confiance insuffisante: {confidence:.0%} "
                    f"| Ratio achat: {buy_ratio:.0%} | OrderFlow: {orderflow_bias}"
                )

        # CAS 3: Position intermédiaire (33-66%) → JAMAIS d'inversion
        elif not in_upper and not in_lower:
            _probe(
                self.log,
                f"[RANGE_REVERSAL] ❌ PAS D'INVERSION | Position intermédiaire: {range_pos:.0%} "
                f"| Garder: {direction}"
            )
            return (direction, False)

        return (reversed_direction, was_reversed) 
    
    # -------------- 6) Decision Generator --------------
    def _final_decision(
        self, mode: str, fused: float, n_vw, coherence, cfg, ctx: Optional[Dict[str, Any]] = None
    ) -> Dict[str, Any]:
        """
        ✅ MISE À JOUR (08 DEC 2025): Ajout logique retournement en range

        Direction finale :
        1. Majorité pondérée (OF + FP + VWAP)
        2. Si égalité → Fallback VWAP bias
        3. Si VWAP neutral → NEUTRAL
        4. ✅ NOUVEAU: Application logique retournement si régime RANGE

        Anchor price :
        - Valeur VWAP (support/résistance dynamique)
        """
        ctx = ctx or {}  # Assurer ctx existe

        # direction finale: majorité pondérée; sinon fallback VWAP bias
        maj = coherence["majority"]

        if maj > 0:
            direction = "BUY"
        elif maj < 0:
            direction = "SELL"
        else:
            # Fallback: utiliser VWAP bias
            vwap_bias = n_vw.get("bias", "neutral").upper()
            if vwap_bias == "BULLISH":
                direction = "BUY"
            elif vwap_bias == "BEARISH":
                direction = "SELL"
            else:
                direction = "NEUTRAL"

        # ✅ AJOUT (08 DEC 2025): Appliquer logique de retournement en range
        original_direction = direction
        direction, was_reversed = self._apply_range_reversal_logic(direction, ctx, n_vw)

        if was_reversed:
            _probe(
                self.log,
                f"[RANGE_REVERSAL] ✅ Direction finale inversée: {original_direction} → {direction}"
            )

        # Anchor price = VWAP value (support/resistance dynamique)
        anchor_price = n_vw.get("value")  # Peut être None si VWAP non disponible

        if mode == "HOLD":
            return {
                "action": "HOLD",
                "signal_type": "WAIT_CONFIRMATION",
                "direction": "NEUTRAL",
                "anchor_price": anchor_price,
            }

        # Récupération des seuils dynamiques depuis la configuration
        thresholds = _get_thresholds(cfg)
        # ✅ FIX (19 DEC 2025) : Aucun fallback - respecter config strictement
        high_threshold = thresholds["high"]
        moderate_threshold = thresholds["moderate"]
        cautious_threshold = thresholds["cautious"]
        conditional_threshold = thresholds["conditional"]
        allow_conditional = thresholds["allow_conditional"]

        # 🔍 DEBUG LOG CRITIQUE - Décision finale
        _probe(
            self.log,
            f"[DECISION_FINALE] fused={fused:.3f} direction={direction} | "
            f"Seuils: high={high_threshold} moderate={moderate_threshold} cautious={cautious_threshold} "
            f"conditional={conditional_threshold} | allow_conditional={allow_conditional}"
        )

        # ================================================================
        # 🚨 FILTRE VETO MOMENTUM INSTITUTIONNEL (23 DEC 2025 - CORRIGÉ)
        # ================================================================
        # Bloquer trades incohérents avec le momentum (ex: BUY sur 7 bougies rouges)
        # ✅ FIX: Bloquer si momentum FORT et OPPOSÉ à la direction (logique inversée corrigée)

        # Lire config veto_rules
        veto_cfg = cfg.get("veto_rules", {})
        momentum_veto_cfg = veto_cfg.get("momentum_veto", {})
        momentum_veto_enabled = momentum_veto_cfg.get("enabled", True)

        momentum_result = ctx.get("momentum_result", {})
        if momentum_result and momentum_veto_enabled:
            mom_score = momentum_result.get("total_score", 100)  # Default 100 si pas dispo
            mom_direction = momentum_result.get("direction", "NEUTRAL")
            mom_quality = momentum_result.get("quality", "N/A")

            # ✅ SEUIL DEPUIS CONFIG: Bloquer si momentum opposé ET score > threshold
            # Logique: Plus le momentum opposé est FORT, plus il doit bloquer le trade
            MOMENTUM_VETO_THRESHOLD = momentum_veto_cfg.get("strong_threshold", 45.0)
            MOMENTUM_MIN_OPPOSITE = momentum_veto_cfg.get("moderate_threshold", 30.0)

            # VETO 1: BUY avec momentum BEARISH FORT (LOGIQUE CORRIGÉE)
            if direction == "BUY" and mom_direction == "BEARISH" and mom_score > MOMENTUM_VETO_THRESHOLD:
                _probe(
                    self.log,
                    f"[MOMENTUM_VETO] ❌ BUY BLOQUÉ | Momentum={mom_direction} Score={mom_score:.1f}/100 > {MOMENTUM_VETO_THRESHOLD} | "
                    f"Quality={mom_quality} | Raison: Momentum bearish FORT, incohérent avec signal BUY (7+ bougies rouges)"
                )
                return {
                    "action": "HOLD",
                    "signal_type": "MOMENTUM_VETO_BUY",
                    "direction": "NEUTRAL",
                    "anchor_price": anchor_price,
                    "veto_reason": f"Momentum {mom_direction} {mom_score:.0f}/100 trop fort, incompatible avec BUY"
                }

            # VETO 2: SELL avec momentum BULLISH FORT (LOGIQUE CORRIGÉE)
            if direction == "SELL" and mom_direction == "BULLISH" and mom_score > MOMENTUM_VETO_THRESHOLD:
                _probe(
                    self.log,
                    f"[MOMENTUM_VETO] ❌ SELL BLOQUÉ | Momentum={mom_direction} Score={mom_score:.1f}/100 > {MOMENTUM_VETO_THRESHOLD} | "
                    f"Quality={mom_quality} | Raison: Momentum bullish FORT, incohérent avec signal SELL (7+ bougies vertes)"
                )
                return {
                    "action": "HOLD",
                    "signal_type": "MOMENTUM_VETO_SELL",
                    "direction": "NEUTRAL",
                    "anchor_price": anchor_price,
                    "veto_reason": f"Momentum {mom_direction} {mom_score:.0f}/100 trop fort, incompatible avec SELL"
                }

            # VETO 3: Directions opposées même si momentum faible/moyen (30-45 pts)
            # Pour éviter les trades contre-tendance même sur momentum modéré
            if (direction == "BUY" and mom_direction == "BEARISH" and mom_score >= MOMENTUM_MIN_OPPOSITE) or \
               (direction == "SELL" and mom_direction == "BULLISH" and mom_score >= MOMENTUM_MIN_OPPOSITE):
                _probe(
                    self.log,
                    f"[MOMENTUM_WARNING] ⚠️ {direction} avec momentum {mom_direction} modéré | Score={mom_score:.1f}/100 | "
                    f"Quality={mom_quality} | Pénalité de confiance appliquée"
                )
                # Pas de VETO strict, mais attention logguée

            # Log si momentum OK (aligné ou neutre)
            if mom_direction != "NEUTRAL":
                # Vérifier cohérence
                is_coherent = (direction == "BUY" and mom_direction == "BULLISH") or \
                              (direction == "SELL" and mom_direction == "BEARISH")

                if is_coherent:
                    _probe(
                        self.log,
                        f"[MOMENTUM_CHECK] ✅ Momentum ALIGNÉ | Direction={mom_direction} Score={mom_score:.1f}/100 "
                        f"Quality={mom_quality} | Cohérent avec {direction}"
                    )
                elif mom_score <= MOMENTUM_MIN_OPPOSITE:
                    _probe(
                        self.log,
                        f"[MOMENTUM_CHECK] ⚠️ Momentum opposé FAIBLE | Direction={mom_direction} Score={mom_score:.1f}/100 "
                        f"Quality={mom_quality} | Trade {direction} accepté avec prudence"
                    )

        # ================================================================
        # 🚨 VETO MTF ALIGNMENT M1/M3 (23 DEC 2025)
        # ================================================================
        # Forcer l'alignement M1 et M3 pour éviter les faux signaux
        # M1 = micro momentum (8 bougies), M3 = confirmation burst (6 bougies)

        # Lire config MTF alignment veto
        mtf_veto_cfg = veto_cfg.get("mtf_alignment_veto", {})
        mtf_veto_enabled = mtf_veto_cfg.get("enabled", True)
        require_m1_m3_aligned = mtf_veto_cfg.get("require_m1_m3_aligned", True)
        allow_neutral = mtf_veto_cfg.get("allow_neutral", True)

        if momentum_result and mtf_veto_enabled and require_m1_m3_aligned:
            mtf_details = momentum_result.get("mtf_details", {})
            m1_direction = mtf_details.get("m1_direction", "NEUTRAL")
            m3_direction = mtf_details.get("m3_direction", "NEUTRAL")
            m5_direction = mtf_details.get("m5_direction", "NEUTRAL")

            # VETO si M1 et M3 ne sont PAS alignés (et non neutres)
            # Permet trades seulement si: M1=M3 OU l'un des deux est NEUTRAL (si allow_neutral=True)
            if allow_neutral:
                m1_m3_aligned = (
                    m1_direction == m3_direction or  # Alignés
                    m1_direction == "NEUTRAL" or     # M1 neutre
                    m3_direction == "NEUTRAL"        # M3 neutre
                )
            else:
                # Mode strict: M1 et M3 DOIVENT être identiques et non neutres
                m1_m3_aligned = (m1_direction == m3_direction and m1_direction != "NEUTRAL")

            if not m1_m3_aligned:
                # M1 et M3 sont opposés → VETO
                _probe(
                    self.log,
                    f"[MTF_VETO] ❌ {direction} BLOQUÉ | M1={m1_direction} ≠ M3={m3_direction} | "
                    f"M5={m5_direction} | Raison: M1 et M3 doivent être alignés pour confirmer le setup"
                )
                return {
                    "action": "HOLD",
                    "signal_type": "MTF_ALIGNMENT_VETO",
                    "direction": "NEUTRAL",
                    "anchor_price": anchor_price,
                    "veto_reason": f"M1 {m1_direction} ≠ M3 {m3_direction} - pas d'alignement"
                }

            # Log si alignement OK
            if m1_direction != "NEUTRAL" or m3_direction != "NEUTRAL":
                if m1_direction == m3_direction and m1_direction != "NEUTRAL":
                    _probe(
                        self.log,
                        f"[MTF_CHECK] ✅ M1/M3 ALIGNÉS | M1={m1_direction} == M3={m3_direction} | M5={m5_direction} | "
                        f"Confirmation setup solide"
                    )
                else:
                    _probe(
                        self.log,
                        f"[MTF_CHECK] ⚠️ M1/M3 Accepté | M1={m1_direction} | M3={m3_direction} (un neutre) | M5={m5_direction}"
                    )

        # ================================================================
        # DÉCISION FINALE PAR SEUILS
        # ================================================================
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

        # ✅ FIX (05 DEC 2025): SEUIL CONDITIONAL - Si score >= 0.35 ET allow_conditional=True → TRADE
        if allow_conditional and fused >= conditional_threshold and direction in ("BUY", "SELL"):
            _probe(self.log, f"[DECISION_FINALE] ✅ CONDITIONAL {direction} AUTORISÉ (score={fused:.3f} >= {conditional_threshold})")
            return {
                "action": direction,
                "signal_type": f"CONDITIONAL_{direction}",
                "direction": direction,
                "anchor_price": anchor_price,
            }

        # Si on arrive ici, le score est trop bas ou allow_conditional=False
        _probe(
            self.log,
            f"[DECISION_FINALE] ❌ HOLD - score={fused:.3f} < conditional={conditional_threshold} "
            f"OU allow_conditional={allow_conditional}"
        )
        return {
            "action": "HOLD",
            "signal_type": "WAIT_CONFIRMATION",
            "direction": "NEUTRAL",
            "anchor_price": anchor_price,
        }

    # -------------- 7) Rationale Builder --------------
    def _rationale(self, decision, n_of, n_fp, n_vw, coherence, rules_eval) -> str:
        """
        ✅ MISE À JOUR (04 DEC 2025): Triggers supprimés, remplacés par VWAP
        """
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

        # VWAP remplace triggers
        vwap_bias = n_vw.get("bias", "neutral")
        vwap_txt = f"vwap {vwap_bias} (score={n_vw.get('score', 0.0):.2f})"

        parts += [
            f"orderflow {dir_of} (score={n_of['score']:.2f}, Δ={n_of['delta_total']:.2f})",
            f"footprint {dir_fp}{' avec ABSORPTION' if n_fp['absorption'] else ''}",
            vwap_txt,
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
        if "n_of" in kw or "n_fp" in kw or "n_vw" in kw:
            out["components"] = {
                "orderflow": kw.get("n_of"),
                "footprint": kw.get("n_fp"),
                "vwap": kw.get("n_vw"),
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
