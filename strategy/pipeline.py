# strategy/pipeline.py
# -*- coding: utf-8 -*-
"""
Pipeline(s) de stratégie — module 'desk-pro' robuste
---------------------------------------------------
Contient un orchestrateur pour la stratégie *Scalping* (ScalpingPipeline).
- ZÉRO dépendance à un "PhaseObserver" pour filtrer
- Entrées: patterns/combos (Detectors si dispo), Footprint (si dispo), Orderflow V5 (si dispo)
- Guardrails minimalistes: spread max, tick_count/tick_rate min, conviction OF min
- Résilience forte: toute absence de donnée/module est gérée proprement
- N'utilise PAS de fichiers communs externes (common/*); s'appuie seulement sur ce qui est déjà dans ton projet

API:
    spipe = ScalpingPipeline(config_manager, arbiter=arbiter, logger=logger)
    decision = spipe.run(asset="XAUUSD", context=context, current_config=current_config)

Retour:
    dict décision {action, asset, confidence?, rule_name?, ...} ou {}
"""

from __future__ import annotations
from typing import Any, Dict, Optional, Tuple
import logging
import math

import pandas as pd

# PATCH SP-01 — import robuste Detectors (phase_observer OU racine)
try:
    from phase_observer.detectors import Detectors  # type: ignore
except Exception:
    try:
        from detectors import Detectors  # type: ignore
    except Exception:  # pragma: no cover
        Detectors = None  # type: ignore

try:
    from orderflow.footprint import footprint_validator  # type: ignore
except Exception:  # pragma: no cover
    footprint_validator = None  # type: ignore

try:
    from orderflow.orderflow_v5 import detect_orderflow_v5  # type: ignore
except Exception:  # pragma: no cover
    detect_orderflow_v5 = None  # type: ignore

try:
    # Tes stratégies existantes
    from strategy.scalping import ScalpingStrategy  # type: ignore
except Exception:
    ScalpingStrategy = None  # type: ignore


class ScalpingPipeline:
    """
    Orchestrateur pour la stratégie 'Scalping'.
    - Collecte signaux (combos/patterns + footprint + orderflow v5)
    - Applique les gardes minimales (spread/ticks/conviction)
    - Appelle la logique de décision 'ScalpingStrategy.evaluate_entry(...)'
    - Ne gère NI sizing NI trailing: c'est du ressort du DecisionPipeline/Executor
    """

    def __init__(self, config_manager: Any, arbiter: Any = None, logger: Optional[logging.Logger] = None) -> None:
        self.config_manager = config_manager
        self.arbiter = arbiter
        self.logger = logger or logging.getLogger("ScalpingPipeline")

        # AuditLogger (optionnel) pour anti-spam
        self.audit = getattr(config_manager, "audit_logger", None)

        # Detectors (optionnel)
        self.detectors = None
        if Detectors is not None:
            try:
                self.detectors = Detectors(logger=self.logger, config_manager=config_manager)
            except Exception as e:
                self.logger.warning(f"[INIT] Detectors non initialisé: {e}")
        self.logger.info("[INIT] ScalpingPipeline prêt (phase-free gating).")

    # ------------------------------------------------------------------
    # Public
    # ------------------------------------------------------------------
    def run(self, asset: str, context: Dict[str, Any], current_config: Dict[str, Any]) -> Dict[str, Any]:
        """Exécute le pipeline et retourne une décision ou {}."""
        try:
            if not asset:
                return {}

            # 0) Dataframes marché (M1)
            df_m1 = self._extract_df_m1(context, asset)

            # 1) Signaux déjà présents
            raw_sig = self._get_existing_signals(context, asset)

            # 2) Patterns/combos
            combos, latest_candle_pat = self._detect_patterns(df_m1, asset)

            # 3) Footprint & Orderflow v5
            fp = self._validate_footprint(context, asset)
            of = self._detect_orderflow(context, asset)

            # 4) Fusion signaux
            asset_signals = self._merge_signals(context, asset, raw_sig, combos, latest_candle_pat, fp, of, current_config)

            # 5) Guardrails minimaux (pas de PhaseObserver gating)
            if not self._guardrails_ok(asset, asset_signals, current_config):
                self._maybe_log(f"[{asset}] Guardrails → pas de trade (spread/ticks/conviction).", key="SCALP_NO_TRADE_GUARD", ttl=10)
                return {}

            # 6) Appel stratégie scalping
            if ScalpingStrategy is None:
                self.logger.error("[PIPE] ScalpingStrategy indisponible.")
                return {}

            decision = self._call_strategy(asset, context, asset_signals, current_config)

            # 7) Pre-gate Arbiter (monitor only ici, le gate enforce est dans DecisionPipeline)
            if decision and self.arbiter and decision.get("action") in {"BUY", "SELL"}:
                try:
                    ok, reason = self.arbiter.can_open(asset, decision["action"], str(current_config.get("strategy_name","scalping")).lower())
                    if not ok:
                        self._maybe_log(f"[ARB.MON] {asset} {decision['action']} serait bloqué: {reason}", key="SCALP_ARB_MONITOR", ttl=5)
                except Exception as e:
                    self.logger.debug(f"[ARB.MON] Ignoré: {e}")

            return decision if isinstance(decision, dict) else {}

        except Exception as e:
            self.logger.error(f"[PIPE] run error on {asset}: {e}", exc_info=True)
            return {}

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------
    def _call_strategy(self, asset: str, context: Dict[str, Any], asset_signals: Dict[str, Any], current_config: Dict[str, Any]) -> Dict[str, Any]:
        """Appelle ScalpingStrategy.evaluate_entry avec compat des différentes signatures existantes."""
        try:
            strat = ScalpingStrategy(self.config_manager, current_config)
        except TypeError:
            strat = ScalpingStrategy(self.config_manager, strategy_config=current_config)  # compat

        # Essais multi-signatures
        errors = []
        for sig in (
            lambda: strat.evaluate_entry(asset, context, asset_signals),
            lambda: strat.evaluate_entry(asset, self._extract_df_m1(context, asset), asset_signals, context, current_config),
            lambda: strat.evaluate_entry(asset, asset_signals),
        ):
            try:
                out = sig()
                if isinstance(out, dict):
                    return out
            except Exception as e:
                errors.append(str(e))
                continue

        self.logger.debug(f"[PIPE] evaluate_entry aucun format n'a abouti. Errors={errors}")
        return {}

    def _extract_df_m1(self, context: Dict[str, Any], asset: str) -> Optional[pd.DataFrame]:
        md = (context.get("market_data") or {}).get(asset, {}) or {}
        for key in ("annotated_rates_df", "rates_df", "df_m1", "df"):
            df = md.get(key)
            if isinstance(df, pd.DataFrame) and not df.empty:
                return df
        return None

    def _get_existing_signals(self, context: Dict[str, Any], asset: str) -> Dict[str, Any]:
        try:
            sig = (context.get("trading_signals") or {}).get(asset, {}) or {}
            return dict(sig) if isinstance(sig, dict) else {}
        except Exception:
            return {}

    def _detect_patterns(self, df_m1: Optional[pd.DataFrame], asset: str) -> Tuple[Optional[Dict[str, Any]], Optional[Dict[str, Any]]]:
        combos = None
        latest = None
        try:
            if self.detectors and isinstance(df_m1, pd.DataFrame) and len(df_m1) >= 30:
                res = self.detectors.detect_combos(df_m1.copy())
                if isinstance(res, dict) and res:
                    combos = res
                if isinstance(res, dict):
                    cands = res.get("candles") or res.get("latest") or None
                    if isinstance(cands, list) and cands:
                        latest = cands[-1] if isinstance(cands[-1], dict) else None
                    elif isinstance(cands, dict):
                        latest = cands
        except Exception as e:
            self.logger.debug(f"[{asset}] detect_combos skipped: {e}")
        return combos, latest

    def _validate_footprint(self, context: Dict[str, Any], asset: str) -> Optional[Dict[str, Any]]:
        # 1) Déjà au contexte ?
        try:
            fp_ctx = (context.get("footprint") or {}).get(asset)
            if isinstance(fp_ctx, dict) and fp_ctx:
                return fp_ctx
        except Exception:
            pass
        # 2) Calcul si possible
        try:
            if callable(footprint_validator):
                md = (context.get("market_data") or {}).get(asset, {}) or {}
                candles = md.get("rates_df") or md.get("df_m1")
                ticks = md.get("ticks_m1") or md.get("ticks_window")
                if isinstance(candles, pd.DataFrame) and len(candles) >= 10:
                    return footprint_validator(candles, ticks)
        except Exception as e:
            self.logger.debug(f"[{asset}] footprint_validator skipped: {e}")
        return None

    def _detect_orderflow(self, context: Dict[str, Any], asset: str) -> Optional[Dict[str, Any]]:
        # 1) Déjà au contexte ?
        try:
            of_ctx = (context.get("orderflow") or {}).get(asset)
            if isinstance(of_ctx, dict) and of_ctx:
                return of_ctx
        except Exception:
            pass
        # 2) Calcul si possible
        try:
            if callable(detect_orderflow_v5):
                md = (context.get("market_data") or {}).get(asset, {}) or {}
                ticks_window = md.get("ticks_window") or md.get("ticks_m1")
                if ticks_window is not None:
                    return detect_orderflow_v5(ticks_window)
        except Exception as e:
            self.logger.debug(f"[{asset}] detect_orderflow_v5 skipped: {e}")
        return None

    def _merge_signals(
        self,
        context: Dict[str, Any],
        asset: str,
        raw_sig: Dict[str, Any],
        combos: Optional[Dict[str, Any]],
        latest_candle_pat: Optional[Dict[str, Any]],
        fp: Optional[Dict[str, Any]],
        of: Optional[Dict[str, Any]],
        cfg: Dict[str, Any],
    ) -> Dict[str, Any]:
        sig = dict(raw_sig or {})

        # Ajout patterns/combos
        if isinstance(combos, dict) and combos:
            sig["combos"] = combos
        if isinstance(latest_candle_pat, dict) and latest_candle_pat:
            sig.setdefault("latest_pattern", latest_candle_pat)

        # Footprint
        if isinstance(fp, dict) and fp:
            sig["footprint_summary"] = fp
            try:
                sig["footprint_status"] = str(fp.get("status", "UNKNOWN")).upper()
                sig["footprint_score"] = float(fp.get("score", 0.0))
                # ticks metrics si présents
                for k in ("tick_count", "tick_rate"):
                    if k in fp and isinstance(fp[k], (int, float)):
                        sig[k] = fp[k]
            except Exception:
                pass

        # Orderflow v5
        if isinstance(of, dict) and of:
            sig["orderflow_v5"] = of
            try:
                s = of.get("summary", {})
                sig["of_bias"] = str(s.get("bias", "")).upper() if isinstance(s, dict) else None
                sig["of_conviction"] = float(s.get("conviction", 0.0)) if isinstance(s, dict) else 0.0
            except Exception:
                pass

        # Spread courant: tenter depuis context.market_data.symbol_info
        md_asset = (context.get("market_data") or {}).get(asset, {}) or {}
        si = (md_asset.get("symbol_info") or cfg.get("symbol_info") or {}) or {}
        try:
            if "spread_pips" not in sig:
                sp_points = md_asset.get("spread_points") or sig.get("spread_points")
                if isinstance(sp_points, (int, float)):
                    point = float(si.get("point") or 0.0001)
                    digits = int(si.get("digits") or 5)
                    pip_points = 10.0 if digits in (3, 5) else 1.0
                    if pip_points > 0:
                        sig["spread_pips"] = float(sp_points) / pip_points
        except Exception:
            pass

        return sig
    
    # --- PATCH GR-02: résolveur guardrails (config + overrides runtime) ---
    @staticmethod
    def _safe_float(v, d: float = 0.0) -> float:
        try:
            v = float(v)
            return v if math.isfinite(v) else d
        except Exception:
            return d

    @staticmethod
    def _safe_int(v, d: int = 0) -> int:
        try:
            return int(v)
        except Exception:
            return d

    @staticmethod
    def _get_pip_points_from_cfg(cfg: Dict[str, Any]) -> float:
        try:
            si = cfg.get("symbol_info") or {}
            digits = int(si.get("digits", 5))
        except Exception:
            digits = 5
        return 10.0 if digits in (3, 5) else 1.0

    @staticmethod
    def _resolve_spread_limit_pips_from_cfg(cfg: Dict[str, Any], asset: str) -> Optional[float]:
        """
        Cherche un seuil de spread en PIPS en priorité:
          1) guardrails.per_symbol[asset].spread.max_pips
          2) guardrails.spread.max_pips / max_spread_pips
          3) scalping.guardrails.spread.max_pips / max_spread_pips
          4) legacy points -> convertit en pips: spread_max_pts / max_spread_points (racine ou scalping)
        """
        pip_points = ScalpingPipeline._get_pip_points_from_cfg(cfg)

        def _first_pips(d: Dict[str, Any]) -> Optional[float]:
            for k in ("max_pips", "spread_max_pips", "max_spread_pips"):
                if isinstance(d, dict) and k in d:
                    v = ScalpingPipeline._safe_float(d.get(k), -1.0)
                    if v > 0:
                        return v
            return None

        def _first_points_to_pips(d: Dict[str, Any]) -> Optional[float]:
            for k in ("spread_max_pts", "max_spread_points"):
                if isinstance(d, dict) and k in d:
                    pts = ScalpingPipeline._safe_float(d.get(k), -1.0)
                    if pts > 0 and pip_points > 0:
                        return pts / pip_points
            return None

        g = cfg.get("guardrails", {}) or {}
        gscalp = (cfg.get("scalping", {}) or {}).get("guardrails", {}) or {}
        per = (g.get("per_symbol", {}) or {}).get(asset, {}) or {}

        # 1) per symbol pips
        v = _first_pips((per.get("spread") or per))
        if v is not None:
            return v

        # 2) global pips
        v = _first_pips((g.get("spread") or g))
        if v is not None:
            return v

        # 3) scalping pips
        v = _first_pips((gscalp.get("spread") or gscalp))
        if v is not None:
            return v

        # 4) legacy points -> pips
        v = _first_points_to_pips(cfg)
        if v is not None:
            return v
        v = _first_points_to_pips(cfg.get("scalping", {}) or {})
        return v

    def _resolve_guardrails(self, asset: str, cfg: Dict[str, Any], context: Dict[str, Any]) -> Dict[str, Any]:
        """
        Renvoie un dict:
          {
            "enabled": bool,
            "spread_limit_pips": Optional[float],
            "min_tick_count": int,
            "min_tick_rate": float,
            "min_conviction": float,
          }
        Priorités: overrides runtime > per_symbol > guardrails globaux > scalping.guardrails > legacy clés
        """
        # Overrides runtime (facultatif)
        rt = (context.get("overrides") or {}).get("scalping_guardrails", {}) or {}
        rt_sym = (rt.get("per_symbol", {}) or {}).get(asset, {}) or {}

        # Config
        g = (cfg.get("guardrails") or {})
        sg = ((cfg.get("scalping") or {}).get("guardrails") or {})
        per = (g.get("per_symbol", {}) or {}).get(asset, {}) or {}

        def _pick(keys, *scopes, cast=float, default=None):
            for key in keys:
                for scope in (rt_sym, rt, per, g, sg, cfg.get("scalping", {}), cfg):
                    if isinstance(scope, dict) and key in scope:
                        try:
                            val = cast(scope[key])
                            if (isinstance(val, (int, float)) and val > 0) or isinstance(val, bool):
                                return val
                        except Exception:
                            pass
            return default

        enabled = _pick(("enabled",), cast=bool, default=True)
        spread_limit_pips = _pick(("spread_max_pips", "max_spread_pips", "max_pips"), default=None)
        if spread_limit_pips is None:
            spread_limit_pips = self._resolve_spread_limit_pips_from_cfg(cfg, asset)

        min_tick_count = _pick(("min_tick_count",), cast=int, default=self._safe_int(cfg.get("min_tick_count", 0)))
        min_tick_rate = _pick(("min_tick_rate",), cast=float, default=self._safe_float(cfg.get("min_tick_rate", 0.0)))
        min_conviction = _pick(("min_of_conviction", "orderflow_min_conviction"), cast=float,
                               default=self._safe_float(cfg.get("min_of_conviction", 0.0)))

        return {
            "enabled": bool(enabled),
            "spread_limit_pips": spread_limit_pips,
            "min_tick_count": int(min_tick_count),
            "min_tick_rate": float(min_tick_rate),
            "min_conviction": float(min_conviction),
        }

    def _guardrails_ok(self, asset: str, sig: Dict[str, Any], cfg: Dict[str, Any]) -> bool:
        # Résoudre dynamiquement depuis config + overrides runtime
        gr = self._resolve_guardrails(asset, cfg, context={})  # placeholder si pas de context ici

        # lecture overrides légères depuis 'sig' si présentes (option)
        try:
            over = sig.get("guardrails_override") or {}
            if isinstance(over, dict):
                if "enabled" in over:
                    gr["enabled"] = bool(over["enabled"])
                if "spread_limit_pips" in over:
                    gr["spread_limit_pips"] = self._safe_float(over["spread_limit_pips"], gr["spread_limit_pips"] or 0.0)
                if "min_tick_count" in over:
                    gr["min_tick_count"] = self._safe_int(over["min_tick_count"], gr["min_tick_count"])
                if "min_tick_rate" in over:
                    gr["min_tick_rate"] = self._safe_float(over["min_tick_rate"], gr["min_tick_rate"])
                if "min_conviction" in over:
                    gr["min_conviction"] = self._safe_float(over["min_conviction"], gr["min_conviction"])
        except Exception:
            pass

        if not gr["enabled"]:
            return True  # garde-fous désactivés -> on laisse passer

        # 1) Spread
        spread_limit_pips = gr["spread_limit_pips"]
        if spread_limit_pips is not None:
            sp = self._extract_spread_pips(sig, cfg)  # sp en pips
            if sp is not None and sp > spread_limit_pips:
                return False

        # 2) Ticks
        if gr["min_tick_count"] > 0 or gr["min_tick_rate"] > 0.0:
            try:
                cnt = self._safe_float(sig.get("tick_count"), 0.0)
                rate = self._safe_float(sig.get("tick_rate"), 0.0)
            except Exception:
                cnt, rate = 0.0, 0.0
            if gr["min_tick_count"] > 0 and cnt < gr["min_tick_count"]:
                return False
            if gr["min_tick_rate"] > 0.0 and rate < gr["min_tick_rate"]:
                return False

        # 3) Orderflow conviction
        if gr["min_conviction"] > 0.0:
            of_conv = self._safe_float(sig.get("of_conviction"), 0.0)
            if of_conv < gr["min_conviction"]:
                return False

        return True

    # ------------------------ helpers ------------------------
    def _extract_spread_pips(self, sig: Dict[str, Any], cfg: Dict[str, Any]) -> Optional[float]:
        try:
            if isinstance(sig.get("spread_pips"), (int, float)):
                return float(sig["spread_pips"])
        except Exception:
            pass
        try:
            si = cfg.get("symbol_info") or {}
            digits = int(si.get("digits", 5))
            pip_points = 10.0 if digits in (3, 5) else 1.0
            for k in ("current_spread_points", "spread_points", "spread"):
                v = sig.get(k)
                if isinstance(v, (int, float)) and pip_points > 0:
                    return float(v) / pip_points
        except Exception:
            pass
        return None

    def _ticks_ok(self, sig: Dict[str, Any], min_count: int, min_rate: float) -> bool:
        try:
            cnt = float(sig.get("tick_count", 0.0))
            rate = float(sig.get("tick_rate", 0.0))
        except Exception:
            cnt, rate = 0.0, 0.0

        if min_count > 0 and cnt < min_count:
            return False
        if min_rate > 0.0 and rate < min_rate:
            return False
        return True

    def _allow_once(self, key: str, ttl: int = 10) -> bool:
        try:
            if self.audit and hasattr(self.audit, "allow_once"):
                return bool(self.audit.allow_once(key, ttl_s=int(ttl)))
        except Exception:
            pass
        return True

    def _maybe_log(self, msg: str, key: str, ttl: int = 10) -> None:
        if self._allow_once(key, ttl):
            self.logger.info(msg)
