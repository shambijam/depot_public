# strategy/pipeline.py
# -*- coding: utf-8 -*-
"""
Pipeline(s) de stratégie — module 'desk-pro' robuste
---------------------------------------------------
Contient un orchestrateur pour la stratégie *Scalping* (ScalpingPipeline).
- ZÉRO dépendance à un "PhaseObserver" pour filtrer
- Entrées: patterns/combos (Detectors si dispo), Footprint (si dispo), Orderflow V5 (si dispo)
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

# === [ORDERFLOW V6 SUPPRIMÉ - Session 28 Nov 2025] ===
# detect_orderflow_v6 supprimé → analyse intégrée dans ScalpingStrategy
# from phase_observer.detect_orderflow_v6.orderflow_v6 import detect_orderflow_v6

try:
    # Tes stratégies existantes
    from strategy.scalping import ScalpingStrategy  # type: ignore
except Exception:
    ScalpingStrategy = None  # type: ignore


class ScalpingPipeline:
    """
    Orchestrateur pour la stratégie 'Scalping'.
    - Collecte signaux (combos/patterns + footprint + orderflow v5)
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
        """Exécute le pipeline et retourne une décision (ou {}). VERSION SANS GUARDRails PIPELINE."""
        try:
            if not asset:
                return {}

            # 0) Dataframes marché (M1)
            df_m1 = self._extract_df_m1(context, asset)

            # --- Merge config par-asset -> current_config
            try:
                asset_cfg = (context.get("asset_configs", {}) or {}).get(asset, {}) or {}
                if asset_cfg:
                    from copy import deepcopy

                    def _deep_merge(a, b):
                        for k, v in (b or {}).items():
                            if isinstance(v, dict) and isinstance(a.get(k), dict):
                                a[k] = _deep_merge(a[k], v)
                            else:
                                a[k] = v
                        return a

                    current_config = _deep_merge(deepcopy(current_config or {}), asset_cfg)
            except Exception as _e:
                self.logger.debug(f"[CFG] Merge asset_cfg skipped: {_e}")

            # 1) Signaux déjà présents (amont)
            raw_sig = self._get_existing_signals(context, asset)

            # === [PATTERNS/COMBOS SUPPRIMÉ - Session 23 Nov 2025] ===
            # Appel _detect_patterns retiré (détecteurs de patterns/bougies supprimés)
            combos, latest_candle_pat = None, None

            # 3) Footprint M1 & Orderflow v5 (si dispo)
            fp = self._validate_footprint(context, asset)
            of = self._detect_orderflow(context, asset)

            # 4) Fusion des signaux
            asset_signals = self._merge_signals(
                context, asset, raw_sig, combos, latest_candle_pat, fp, of, current_config
            )

            # 5) [REMOVED] Guardrails pipeline → on s’appuie uniquement sur footprint/orderflow
            self._maybe_log(
                f"[{asset}] Guardrails pipeline supprimés → footprint/orderflow only.",
                key="SCALP_GUARDS_REMOVED",
                ttl=60,
            )

            # 6) Appel stratégie scalping
            if ScalpingStrategy is None:
                self.logger.error("[PIPE] ScalpingStrategy indisponible.")
                return {}

            decision = self._call_strategy(asset, context, asset_signals, current_config)

            # 7) Pre-gate Arbiter (monitor only — pas de blocage ici)
            if decision and self.arbiter and decision.get("action") in {"BUY", "SELL"}:
                try:
                    ok, reason = self.arbiter.can_open(
                        asset,
                        decision["action"],
                        str(current_config.get("strategy_name", "scalping")).lower(),
                    )
                    if not ok:
                        self._maybe_log(
                            f"[ARB.MON] {asset} {decision['action']} serait bloqué: {reason}",
                            key="SCALP_ARB_MONITOR",
                            ttl=5,
                        )
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

    # === [FONCTION _detect_patterns SUPPRIMÉE - Session 23 Nov 2025] ===
    # Fonction _detect_patterns retirée (30 lignes)
    # Raison : Détecteurs de patterns/combos/bougies retirés du système


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
        """
        Orderflow V6 (institutionnel) — lit les bougies M1 du context et renvoie {score,status,summary,patterns}.
        Priorité: annotated_rates_df_m1 > annotated_rates_df > rates_df > ticks_window > ticks_m1.
        """
        # 1) Déjà calculé et présent dans le contexte ?
        try:
            of_ctx = (context.get("orderflow") or {}).get(asset)
            if isinstance(of_ctx, dict) and of_ctx:
                return of_ctx
        except Exception:
            pass

        # 2) Si la V6 est importée/chargeable, on calcule
        try:
            # === [ORDERFLOW V6 SUPPRIMÉ - Session 28 Nov 2025] ===
            # Ancienne analyse detect_orderflow_v6 supprimée
            # → Analyse OrderFlow V6 maintenant intégrée dans ScalpingStrategy._analyze_orderflow_v6()
            pass
        except Exception as e:
            self.logger.debug(f"[{asset}] OrderFlow analysis skipped: {e}", exc_info=False)

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
            sig["orderflow_v6"] = of
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
    
  