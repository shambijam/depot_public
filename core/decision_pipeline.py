# core/decision_pipeline.py
import logging
import json
import pandas as pd
import numpy as np
import math
import time
import traceback
from datetime import datetime, UTC, timezone
from typing import Dict, Any, List, Optional, Tuple, TYPE_CHECKING
from pathlib import Path
from core.ai_interface import AIInterface
from core.utils import ConfigValidationError, TradeStatus
from typing import Any, Dict, List, Optional, Tuple
from strategy.scalping import ScalpingStrategy
from strategy.liquidity import LiquidityStrategy
from core.utils import normalize_levels
from sniper_patterns.pattern_engine import PatternEngine


# Utilisation de TYPE_CHECKING pour éviter les importations circulaires à l'exécution
if TYPE_CHECKING:
    from core.config_manager import (
        ConfigManager,
    )  # Importation uniquement pour les hints de type

logger = logging.getLogger(__name__)


class DecisionPipeline:
    """
    Orchestre le pipeline de décision de trading, intégrant l'analyse de marché,
    la sélection des stratégies, l'adaptation dynamique et l'intégration consultative de l'IA.
    C'est le module responsable de la logique de prise de décision institutionnelle.
    """

    def __init__(
        self,
        config_manager_instance,
        ai_interface_instance=None,
        strategy_manager_instance=None,
        phase_observer_instance=None,
    ):
        """
        Initialise le DecisionPipeline sans présumer de la présence d'un PhaseObserver.
        - Ne touche PAS à self.phase_observer si None
        - Lit le flag de debug depuis la config, et l'applique uniquement si un PhaseObserver est fourni
        """
        self.logger = logging.getLogger(__name__)
        self.config_manager = config_manager_instance
        self.ai_interface = ai_interface_instance
        self.strategy_manager = strategy_manager_instance
        self.pattern_engine = PatternEngine(
            enable_context=True, enable_structure=True, enable_multi_tf=True
        )

        # ✅ Cache local des configs assets (chargées une seule fois au démarrage)
        self.asset_configs: Dict[str, Dict[str, Any]] = {}
        for asset in ["EURUSD", "GBPUSD", "XAUUSD"]:
            try:
                cfg = self.config_manager.load_asset_config(asset)
                self.asset_configs[asset] = cfg
                self.logger.info(
                    f"[CACHE] Config {asset} chargée une seule fois au démarrage."
                )
            except Exception as e:
                self.logger.error(f"[CACHE] Impossible de charger {asset}.json: {e}")
                self.asset_configs[asset] = {}

        self.logger.info("DecisionPipeline initialisé avec cache asset_configs.")

        # PhaseObserver optionnel (peut être attaché plus tard via attach_phase_observer)
        self.phase_observer = phase_observer_instance

        # Flag de debug (on ne force rien si pas de phase_observer)
        debug_flag = (
            self.config_manager.get(
                "debug_settings.phase_observer.debug_confidence_logging", None
            )
            or self.config_manager.get("phase_observer.debug_confidence_logging", None)
            or self.config_manager.get(
                "phase_detection_defaults.debug_confidence_logging", None
            )
        )
        debug_flag = bool(debug_flag) if isinstance(debug_flag, bool) else False

        # Si un PhaseObserver est fourni et expose l’attribut, on applique
        if self.phase_observer is not None and hasattr(
            self.phase_observer, "debug_confidence_logging"
        ):
            self.phase_observer.debug_confidence_logging = debug_flag

        # Conserver le flag aussi côté pipeline pour logs internes éventuels
        self.debug_confidence_logging = debug_flag

        self.logger.info(
            f"DecisionPipeline initialisé (phase_observer={'present' if self.phase_observer else 'absent'}) "
            f"| debug_confidence_logging={self.debug_confidence_logging}"
        )

    def get_asset_config(self, asset: str) -> Dict[str, Any]:
        """Charge une config asset une seule fois et la met en cache."""
        if asset not in self.asset_configs:
            cfg = self.config_manager.load_asset_config(asset)
            self.asset_configs[asset] = cfg
            self.logger.info(f"[CACHE] Config pour {asset} chargée et stockée.")
        return self.asset_configs[asset]

    def attach_phase_observer(self, phase_observer):
        """Attache/met à jour le PhaseObserver après coup, en appliquant le flag de debug s'il existe."""
        self.phase_observer = phase_observer
        if hasattr(self.phase_observer, "debug_confidence_logging"):
            self.phase_observer.debug_confidence_logging = bool(
                getattr(self, "debug_confidence_logging", False)
            )
        self.logger.info("PhaseObserver attaché au DecisionPipeline.")

    @staticmethod
    def get_max_spread_pips(cfg_scalping: dict, symbol: str) -> float:
        """
        Lit la marge max autorisée en pips depuis la conf scalping.
        Accepte soit une valeur unique, soit un dict par symbole, avec clé 'default'.
        """
        v = (cfg_scalping or {}).get("max_spread_pips", 3.0)
        return v.get(symbol, v.get("default", v)) if isinstance(v, dict) else v
    

    def institutional_decision_pipeline(self, context: Dict[str, Any]) -> Dict[str, Any]:
        """
        Orchestration décisionnelle (Banque Privée)
        - ScalpingStrategy -> XAUUSD
        - LiquidityStrategy -> EURUSD, GBPUSD
        - 1 trade max par cycle (on prend le premier valide)
        """
        import logging
        from datetime import datetime, timezone as _tz

        UTC = _tz.utc

        self.logger.info("--- Démarrage du Pipeline de Décision Institutionnel ---")
        print("🤖 [DECISION] Début du pipeline institutionnel")

        try:
            # === ÉTAPE 1: Analyse du contexte ===
            print("🤖 [DECISION] Étape 1: Analyse du contexte...")
            analyzed_context = self.config_manager.analyze_context(context) or {}
            print("🤖 [DECISION] Contexte analysé avec succès")

            # === Snapshot guardrails / IA (debug) ===
            base_cfg = self.config_manager.get_current_dynamic_config() or {}
            guard = base_cfg.get("guardrails") or {}
            ai_cfg = base_cfg.get("ai", {}) or {}
            print(
                "🧱 [DEBUG] GUARDRAILS SNAPSHOT →",
                f"enabled={(guard or {}).get('enabled')}, ",
                f"vol.min_atr_m1={(guard.get('volatility') or {}).get('min_atr_m1_pips')}, ",
                f"spread.max={(guard.get('spread') or {}).get('max_spread_pips')}, ",
                f"sessions.news_blackout={(guard.get('sessions_news') or {}).get('news_blackout_enabled')}",
            )
            print(
                f"🤖 [DECISION] IA {'activée' if ai_cfg.get('enabled') else 'désactivée'} "
                f"(min_conf={ai_cfg.get('min_confidence')})"
            )

            # === ÉTAPE 2: Dispatch + appels stratégies (via StrategyManager) ===
            print("🤖 [DECISION] Étape 2: Dispatch des stratégies...")
            signals = analyzed_context.get("trading_signals", {}) or {}

            # Dump court des signaux utiles (debug)
            for asset, sig in signals.items():
                spread_pts = sig.get("current_spread_points")
                print(
                    f"🔎 [DEBUG] {asset} → phase={sig.get('phase')} "
                    f"conf={sig.get('confidence_score')} spread_pts={spread_pts}"
                )

            # Conteneurs séparés (séparation stricte des domaines)
            scalping_decisions: list = []
            liquidity_decisions: list = []

            # --- Helpers locaux ---
            def _norm_action(x: str) -> str:
                return (x or "").strip().upper()

            def _is_valid(dec: dict) -> bool:
                return _norm_action(dec.get("action")) in {"BUY", "SELL", "CLOSE"}

            def _ensure_asset(dec: dict, fallback_asset: str) -> None:
                if not dec.get("asset") or str(dec.get("asset")).upper() == "UNKNOWN":
                    dec["asset"] = fallback_asset

            # --- SCALPING (XAUUSD only) ---
            if "XAUUSD" in signals:
                scalping = self.strategy_manager.get_strategy_instance(
                    "scalping",
                    per_asset="XAUUSD",
                    inject={
                        "mt5_connector": getattr(self, "mt5_connector", None),
                        "phase_observer": getattr(self, "phase_observer", None),
                        "ai_interface": getattr(self, "ai_interface", None),
                        "risk_manager": getattr(self, "risk_manager", None),
                        "audit_logger": logging.getLogger("AuditLogger"),
                    },
                    strict=False,
                )
                if scalping:
                    try:
                        dec = scalping.evaluate_entry("XAUUSD", analyzed_context, signals["XAUUSD"])
                        if isinstance(dec, dict):
                            # Cas spécial BURST
                            if "burst_decisions" in dec and isinstance(dec["burst_decisions"], list):
                                for sub_dec in dec["burst_decisions"]:
                                    sub_dec["strategy_type"] = "scalping"
                                    _ensure_asset(sub_dec, "XAUUSD")
                                    sub_dec.setdefault("execution_status", "ready")
                                    if _is_valid(sub_dec):
                                        scalping_decisions.append(sub_dec)
                                        print(f"✅ [SCALPING] burst décision retenue: {sub_dec.get('action')} {sub_dec.get('asset')} idx={sub_dec.get('burst_index')}")
                            else:
                                dec["strategy_type"] = "scalping"
                                _ensure_asset(dec, "XAUUSD")
                                dec.setdefault("execution_status", "ready")
                                if _is_valid(dec):
                                    scalping_decisions.append(dec)
                                    print(f"✅ [SCALPING] décision retenue: {dec.get('action')} {dec.get('asset')} rule={dec.get('rule_name')}")

                    except Exception as e:
                        self.logger.error(f"[DECISION] Erreur scalping: {e}", exc_info=True)

            # --- LIQUIDITY (EURUSD/GBPUSD) ---
            liq_assets = [a for a in ("EURUSD", "GBPUSD") if a in signals]
            if liq_assets:
                liquidity = self.strategy_manager.get_strategy_instance(
                    "liquidity",
                    inject={
                        "mt5_connector": getattr(self, "mt5_connector", None),
                        "phase_observer": getattr(self, "phase_observer", None),
                        "ai_interface": getattr(self, "ai_interface", None),
                        "risk_manager": getattr(self, "risk_manager", None),
                        "audit_logger": logging.getLogger("AuditLogger"),
                    },
                    strict=False,
                )
                if liquidity:
                    try:
                        dec = liquidity.evaluate_entry(
                            analyzed_context, {a: signals[a] for a in liq_assets}
                        )
                        decs = dec if isinstance(dec, list) else ([dec] if isinstance(dec, dict) else [])
                        for d in decs:
                            d["strategy_type"] = "liquidity"
                            _ensure_asset(d, liq_assets[0])
                            d.setdefault("execution_status", "ready")
                            if _is_valid(d):
                                liquidity_decisions.append(d)
                    except Exception as e:
                        self.logger.error(f"[DECISION] Erreur liquidity: {e}", exc_info=True)
                        
                    # === Fusion pour compat héritage (tout en gardant les listes séparées) ===
                    print(f"📦 scalping_decisions={len(scalping_decisions)} | liquidity_decisions={len(liquidity_decisions)}")
                    if scalping_decisions:
                        print(f"   ↳ top scalping: {scalping_decisions[0].get('action')} {scalping_decisions[0].get('asset')}")
                    if liquidity_decisions:
                        print(f"   ↳ top liquidity: {liquidity_decisions[0].get('action')} {liquidity_decisions[0].get('asset')}")

                    final_decisions = scalping_decisions + liquidity_decisions

                    # === ÉTAPE 3: Choix principal (1 trade max / cycle) ===
                    td = final_decisions[0] if final_decisions else {}
                    chosen_strategy = td.get("strategy_type") if td else None
                    chosen_asset = td.get("asset") if td else None
  

            # === Fusion pour compat héritage (tout en gardant les listes séparées) ===
            final_decisions = scalping_decisions + liquidity_decisions

            # === ÉTAPE 3: Choix principal (1 trade max / cycle) ===
            td = final_decisions[0] if final_decisions else {}
            chosen_strategy = td.get("strategy_type") if td else None
            chosen_asset = td.get("asset") if td else None

            # === ÉTAPE 4: Adaptation config (base + config stratégie choisie) ===
            if chosen_strategy:
                strat_cfg = self.strategy_manager.get_strategy_config(chosen_strategy) or {}
            else:
                strat_cfg = {}  # ne JAMAIS appeler get_strategy_config(None)

            config_for_this_cycle = self.config_manager._merge_dicts(base_cfg, strat_cfg)
            adapted_config = self.adapt_config(config_for_this_cycle, analyzed_context) or {}
            print("🤖 [DECISION] Configuration adaptée avec succès")

            # === ÉTAPE 4bis: Execution context (léger) ===
            execution_context = {"spreads_pips": {}, "katana_snapshots": {}, "katana_ready_assets": []}
            analyzed_context["execution_context"] = execution_context

            # === ÉTAPE 5: Affichage décision finale ===
            if not td:
                action_raw, label = "", "AUCUN"
            else:
                action_raw = _norm_action(td.get("action"))
                status = str(td.get("execution_status") or "").lower()
                if action_raw not in {"BUY", "SELL", "CLOSE"}:
                    label = "AUCUN"
                elif status in {"filled", "placed"}:
                    label = "TRADE EXÉCUTÉ"
                elif status == "pending_manual_approval":
                    label = "EN ATTENTE VALIDATION"
                elif status == "ready":
                    label = "PRÊT (DRY RUN)"
                else:
                    label = "TRADE DÉCIDÉ"

            print(f"🤖 [DECISION] Décision finale: {action_raw} | {label}")

            return {
                "timestamp_utc": datetime.now(UTC).isoformat(),
                "context": analyzed_context,
                "config_used": adapted_config,
                # Listes séparées pour exécution indépendante dans run_single_pipeline_cycle
                "scalping_decisions": scalping_decisions,
                "liquidity_decisions": liquidity_decisions,
                # Compat héritage
                "final_decisions": final_decisions,
                "final_decision": td,
                "execution_context": execution_context,
                "decision_trace": [],
            }

        except Exception as e:
            print(f"💥 [DECISION] ERREUR dans le pipeline: {e}")
            self.logger.error(f"Erreur critique dans institutional_decision_pipeline: {e}", exc_info=True)
            return {
                "timestamp_utc": datetime.now(UTC).isoformat(),
                "context": context,
                "config_used": self.config_manager.get_current_dynamic_config(),
                "final_decision": {"action": "AUCUNE", "asset": "NONE"},
                "execution_context": {},
                "error": str(e),
            }


    def adapt_config(
        self, config: Dict[str, Any], context: Dict[str, Any]
    ) -> Dict[str, Any]:
        """
        Adapte dynamiquement les paramètres de la configuration choisie en fonction
        du contexte en temps réel (ex: volatilité, régime de marché).
        Déplacée de ConfigManager.
        """
        self.logger.info("Adaptation de la configuration sélectionnée au contexte...")
        adapted_config = config.copy()

        market_regime = context.get("current_market_regime", "unknown_regime_fallback")
        volatility_percentage = context.get("market_volatility_percentage", 0.0)
        strategy_tags = adapted_config.get("strategy_tags", [])
        strategy_name = adapted_config.get("strategy_name", "general").lower()

        self.logger.debug(
            f"Adaptation pour stratégie '{strategy_name}' (Tags: {strategy_tags}) - Régime: {market_regime}, Volatilité: {volatility_percentage:.2f}%"
        )

        adapted_config = self._adapt_to_volatility(
            adapted_config, volatility_percentage
        )
        adapted_config = self._adapt_to_market_regime(
            adapted_config, market_regime, strategy_tags
        )

        self.logger.info("Adaptation de la configuration terminée.")
        return adapted_config

    def _adapt_to_volatility(
        self, config: Dict[str, Any], volatility_percentage: float
    ) -> Dict[str, Any]:
        """
        Adapte les paramètres de la configuration en fonction de la volatilité actuelle.
        Déplacée de ConfigManager.
        """
        adapted_config = config.copy()
        high_vol_threshold = self.config_manager.get(
            "adaptation_settings.volatility_thresholds.high", 25.0
        )
        low_vol_threshold = self.config_manager.get(
            "adaptation_settings.volatility_thresholds.low", 15.0
        )

        if volatility_percentage > high_vol_threshold:
            self.logger.debug(
                f"Contexte de HAUTE VOLATILITÉ détecté ({volatility_percentage:.2f}% > {high_vol_threshold}%). Application des ajustements."
            )

            if "scalping" in adapted_config.get("strategy_tags", []):
                adapted_config["take_profit_pips"] = self.config_manager.get(
                    "adaptation_settings.scalping.take_profit_pips_high_vol",
                    config.get("take_profit_pips"),
                )
                adapted_config["stop_loss_pips"] = self.config_manager.get(
                    "adaptation_settings.scalping.stop_loss_pips_high_vol",
                    config.get("stop_loss_pips"),
                )
                adapted_config["max_spread_pips"] = self.config_manager.get(
                    "adaptation_settings.scalping.max_spread_pips_high_vol",
                    config.get("max_spread_pips"),
                )
                self.logger.debug(
                    f"  Adaptation Scalping (Haute Vol) : SL/TP ajustés à {adapted_config.get('stop_loss_pips')}/{adapted_config.get('take_profit_pips')} pips, Spread max: {adapted_config.get('max_spread_pips')} pips."
                )

            risk_reduction_multiplier = self.config_manager.get(
                "adaptation_settings.risk_adjustment.risk_reduction_multiplier_high_vol",
                0.8,
            )
            min_risk = self.config_manager.get(
                "adaptation_settings.risk_adjustment.min_risk_percent_after_adjustment",
                0.01,
            )
            original_risk = adapted_config.get("risk_per_trade_percent", 1.0)
            adapted_config["risk_per_trade_percent"] = max(
                min_risk, original_risk * risk_reduction_multiplier
            )
            self.logger.debug(
                f"  Adaptation Risque (Haute Vol) : Risque par trade réduit à {adapted_config['risk_per_trade_percent']:.2f}%."
            )

            # CORRECTION: Initialiser phase_detection si elle n'existe pas
            if "phase_detection" not in adapted_config:
                adapted_config["phase_detection"] = {}

            adapted_config["phase_detection"]["lookback_window"] = (
                self.config_manager.get(
                    "adaptation_settings.phase_detection.lookback_window_high_vol",
                    config.get("phase_detection", {}).get("lookback_window"),
                )
            )
            adapted_config["phase_detection"]["volume_zscore"] = (
                self.config_manager.get(
                    "adaptation_settings.phase_detection.volume_zscore_high_vol",
                    config.get("phase_detection", {}).get("volume_zscore"),
                )
            )
            self.logger.debug(
                f"  Adaptation Phase Detection (Haute Vol) : lookback_window={adapted_config['phase_detection']['lookback_window']}, volume_zscore={adapted_config['phase_detection']['volume_zscore']}."
            )

        elif volatility_percentage < low_vol_threshold:
            self.logger.debug(
                f"Contexte de BASSE VOLATILITÉ détecté ({volatility_percentage:.2f}% < {low_vol_threshold}%). Application des ajustements."
            )

            if "scalping" in adapted_config.get("strategy_tags", []):
                adapted_config["take_profit_pips"] = self.config_manager.get(
                    "adaptation_settings.scalping.take_profit_pips_low_vol",
                    config.get("take_profit_pips"),
                )
                adapted_config["stop_loss_pips"] = self.config_manager.get(
                    "adaptation_settings.scalping.stop_loss_pips_low_vol",
                    config.get("stop_loss_pips"),
                )
                self.logger.debug(
                    f"  Adaptation Scalping (Basse Vol) : SL/TP élargis à {adapted_config.get('stop_loss_pips')}/{adapted_config.get('take_profit_pips')} pips."
                )

            # CORRECTION: Initialiser phase_detection si elle n'existe pas
            if "phase_detection" not in adapted_config:
                adapted_config["phase_detection"] = {}

            adapted_config["phase_detection"]["lookback_window"] = (
                self.config_manager.get(
                    "adaptation_settings.phase_detection.lookback_window_low_vol",
                    config.get("phase_detection", {}).get("lookback_window"),
                )
            )
            adapted_config["phase_detection"]["volume_zscore"] = (
                self.config_manager.get(
                    "adaptation_settings.phase_detection.volume_zscore_low_vol",
                    config.get("phase_detection", {}).get("volume_zscore"),
                )
            )
            self.logger.debug(
                f"  Adaptation Phase Detection (Basse Vol) : lookback_window={adapted_config['phase_detection']['lookback_window']}, volume_zscore={adapted_config['phase_detection']['volume_zscore']}."
            )

        return adapted_config

    def _adapt_to_market_regime(
        self, config: Dict[str, Any], market_regime: str, strategy_tags: List[str]
    ) -> Dict[str, Any]:
        """
        Adapte les paramètres de la configuration en fonction du régime de marché détecté.
        Déplacée de ConfigManager.
        """
        adapted_config = config.copy()

        if "consolidation" in market_regime and "range" in strategy_tags:
            self.logger.debug(
                f"Contexte de CONSOLIDATION détecté. Adaptation pour stratégie de range."
            )
            adapted_config["take_profit_pips"] = self.config_manager.get(
                "adaptation_settings.market_regime_specific.consolidation.take_profit_pips",
                config.get("take_profit_pips"),
            )
            adapted_config["stop_loss_pips"] = self.config_manager.get(
                "adaptation_settings.market_regime_specific.consolidation.stop_loss_pips",
                config.get("stop_loss_pips"),
            )

            # CORRECTION: Initialiser strategy_toggles si elle n'existe pas
            if "strategy_toggles" not in adapted_config:
                adapted_config["strategy_toggles"] = {}

            adapted_config["strategy_toggles"]["enable_grid_in_range_only"] = (
                self.config_manager.get(
                    "adaptation_settings.market_regime_specific.consolidation.enable_grid_in_range_only",
                    adapted_config.get("strategy_toggles", {}).get(
                        "enable_grid_in_range_only"
                    ),
                )
            )

        elif "trending" in market_regime and "trend_following" in strategy_tags:
            self.logger.debug(
                f"Contexte de TENDANCE détecté. Adaptation pour stratégie de suivi de tendance."
            )
            adapted_config["take_profit_pips"] = self.config_manager.get(
                "adaptation_settings.market_regime_specific.trending.take_profit_pips",
                config.get("take_profit_pips"),
            )
            adapted_config["stop_loss_pips"] = self.config_manager.get(
                "adaptation_settings.market_regime_specific.trending.stop_loss_pips",
                config.get("stop_loss_pips"),
            )
            adapted_config["enable_trailing_stop"] = self.config_manager.get(
                "adaptation_settings.market_regime_specific.trending.enable_trailing_stop",
                adapted_config.get("enable_trailing_stop"),
            )
            adapted_config["risk_per_trade_percent"] = self.config_manager.get(
                "adaptation_settings.market_regime_specific.trending.risk_per_trade_percent",
                config.get("risk_per_trade_percent"),
            )

        return adapted_config

    def decide_exit_trades(
        self,
        context: Dict[str, Any],
        open_positions: List[Dict[str, Any]],
        active_config: Dict[str, Any],
        strategy_manager_instance,
    ) -> List[Dict[str, Any]]:
        """
        Décide des exits basés uniquement sur la logique des stratégies.
        ❌ Aucun fallback générique (breakeven, trailing, time-stop).
        """
        exit_decisions: List[Dict[str, Any]] = []

        for pos in open_positions:
            magic = self._pos_magic(pos)
            if magic is None:
                continue

            # Récupérer la stratégie associée
            strat = strategy_manager_instance.get_strategy_by_magic(magic)
            if strat and hasattr(strat, "evaluate_exit"):
                try:
                    strat_exit = strat.evaluate_exit(context, [pos])
                    if strat_exit:
                        exit_decisions.extend(strat_exit)
                except Exception as e:
                    self.logger.error(
                        f"[EXIT] Erreur dans evaluate_exit de {strat}: {e}"
                    )

        return exit_decisions

    # ---- Helpers locaux robustes ----
    def _pos_id(p: Dict[str, Any]) -> Any:
        return (
            p.get("ticket")
            or p.get("id")
            or p.get("Order")
            or p.get("Position")
            or p.get("position_id")
        )

    def _pos_symbol(p: Dict[str, Any]) -> Optional[str]:
        return p.get("symbol") or p.get("Symbol")

    def _pos_magic(p: Dict[str, Any]) -> Optional[int]:
        try:
            return int(p.get("magic")) if p.get("magic") is not None else None
        except Exception:
            return None

    def _pos_side(p: Dict[str, Any]) -> Optional[str]:
        """
        Retourne 'BUY' ou 'SELL' selon les champs usuels.
        MT5 Nom: type -> 0 buy / 1 sell ; ou string 'POSITION_TYPE_BUY/SELL'
        """
        t = p.get("type") or p.get("Type")
        if isinstance(t, str):
            t = t.upper()
            if "BUY" in t:
                return "BUY"
            if "SELL" in t:
                return "SELL"
        try:
            # MT5: 0 -> BUY ; 1 -> SELL
            t_int = int(t)
            return "BUY" if t_int == 0 else "SELL" if t_int == 1 else None
        except Exception:
            return None

    def _pos_entry(p: Dict[str, Any]) -> Optional[float]:
        for k in ("price_open", "Price", "price"):
            if k in p:
                try:
                    return float(p[k])
                except Exception:
                    pass
        return None

    def _pos_sl(p: Dict[str, Any]) -> Optional[float]:
        for k in ("sl", "StopLoss"):
            if k in p:
                try:
                    return float(p[k])
                except Exception:
                    pass
        return None

    def _pos_tp(p: Dict[str, Any]) -> Optional[float]:
        for k in ("tp", "TakeProfit"):
            if k in p:
                try:
                    return float(p[k])
                except Exception:
                    pass
        return None

    def _pos_volume(p: Dict[str, Any]) -> float:
        for k in ("volume", "Volume"):
            if k in p:
                try:
                    v = float(p[k])
                    return v if v > 0 else 0.01
                except Exception:
                    pass
        return 0.01

    def _digits_info(mkt: Dict[str, Any]) -> Tuple[int, float]:
        si = mkt.get("symbol_info", {}) or {}
        digits = int(si.get("digits", 5))
        point = float(si.get("point", 0.00001) or 0.00001)
        return digits, point

    def _round_to_digits(x: float, digits: int) -> float:
        # arrondit au nombre de décimales "digits"
        try:
            return float(f"{x:.{digits}f}")
        except Exception:
            return x

    def _current_price_for_symbol(sym: str, mkt: Dict[str, Any]) -> Optional[float]:
        cp = mkt.get("current_price")
        if isinstance(cp, (int, float)) and cp > 0:
            return float(cp)
        df = mkt.get("annotated_rates_df")
        if df is not None and len(df) > 0:
            try:
                return float(df["close"].iloc[-1])
            except Exception:
                return None
        return None

    def _bars_since_open(
        sym: str, mkt: Dict[str, Any], open_time: Optional[float]
    ) -> Optional[int]:
        """
        Compte les bougies M1 écoulées depuis l'ouverture (si DF indexé en datetime).
        Fallback: None si impossible.
        """
        df = mkt.get

    def _quantize_volume(
        self, vol: float, vmin: float, vmax: float, vstep: float
    ) -> float:
        """Ajuste le volume aux contraintes broker (min, max, step)."""
        try:
            vol = float(vol)
        except (TypeError, ValueError):
            vol = vmin

        if vol <= 0 or not (vol == vol):  # NaN-safe
            vol = vmin

        # Snap au step
        steps = max(1, round(vol / vstep)) if vstep > 0 else 1
        vol_q = steps * vstep

        # Clamp min/max
        if vol_q < vmin:
            vol_q = vmin
        if vol_q > vmax:
            vol_q = vmax

        # Arrondi propre aux pas courants (2 décimales suffisent pour la plupart des brokers)
        return round(vol_q, 2)

    def decide_trade_to_execute(
        self,
        context: Dict[str, Any],
        current_config: Dict[str, Any],
        signals: Dict[str, Any],
        strategy_manager_instance=None,
    ) -> Dict[str, Any]:
        """
        Prend directement la décision de trade en utilisant les paramètres de stratégie
        mais sans déléguer la décision finale aux instances de stratégie.
        DecisionPipeline est le SEUL DÉCIDEUR.

        ➕ Intégration optionnelle EMA/RSI/ATR Trailing (entrées uniquement)
        - Si activé et qu’une entrée BUY/SELL est proposée, on construit SL/TP (TP via min_rr*SL)
        et on bypass le gate Bollinger. La gestion des EXIT/UPDATE_TRAIL reste au position manager.
        """
        # DIAG local
        try:
            from core.diagnostics import get_tracker_from_context
        except Exception:
            get_tracker_from_context = None

        def _diag_size(asset_sym: str, reason: str, extra: dict | None = None):
            if not get_tracker_from_context:
                return
            try:
                get_tracker_from_context(context).note(
                    asset_sym, "sizing", reason, extra or {}
                )
            except Exception:
                pass

        self.logger.info(
            "CORE DECISION ENGINE - Prise de décision directe sans délégation..."
        )
        self.logger.debug(f"Signaux reçus pour évaluation: {signals}")
        print("🧠 [CORE] Démarrage décision directe (sans délégation)")

        # 1) Filtres pré-décision critiques (sécurité globale)
        if current_config.get(
            "halt_on_major_news", True
        ) and self.config_manager.check_news_schedule(
            context, context.get("economic_calendar", [])
        ):
            msg = "Trade suspendu en raison d'un événement d'actualité majeur."
            self.logger.warning(msg)
            print(f"⛔ [CORE] {msg}")
            self.config_manager.log_decision(
                current_config, {}, context, "Trade bloqué: Actualité majeure."
            )
            return {}

        # 2) Récupérer le nom de stratégie
        strategy_name = current_config.get("strategy_name", "unknown")
        self.logger.info(
            f"🎯 CORE prend la décision avec paramètres de stratégie: {strategy_name}"
        )
        print(f"🎯 [CORE] Stratégie paramétrée: {strategy_name}")

        # 3) CORE évalue directement les signaux (via stratégies dédiées)
        trade_decision = {}

        try:
            from strategy.scalping import ScalpingStrategy
            from strategy.liquidity import LiquidityStrategy
        except Exception as e:
            self.logger.error(f"[CORE] Impossible d'importer les stratégies: {e}")
            print(f"⚠️ [CORE] Erreur import stratégie: {e}")
            return {}

        # --- Priorité 1 : Scalping sur XAUUSD ---
        if "XAUUSD" in signals:
            try:
                strat = ScalpingStrategy(self.config_manager, current_config)
                decision = strat.evaluate_entry(
                    "XAUUSD",
                    context.get("market_data", {}).get("XAUUSD", {}).get("rates_df"),
                    signals.get("XAUUSD", {}),
                    context,
                    current_config,
                )
                if decision:
                    trade_decision = decision
                    self.logger.info("[CORE] Signal scalping retenu sur XAUUSD")
                    print("✅ [CORE] Décision scalping détectée sur XAUUSD")
            except Exception as e:
                self.logger.error(f"[CORE] Erreur evaluate_entry scalping: {e}")

        # --- Priorité 2 : Liquidity sur EURUSD / GBPUSD ---
        if not trade_decision:
            liq_assets = [a for a in ["EURUSD", "GBPUSD"] if a in signals]
            if liq_assets:
                try:
                    strat = LiquidityStrategy(self.config_manager, current_config)
                    decision = strat.evaluate_entry(
                        context, {a: signals[a] for a in liq_assets}
                    )
                    if decision:
                        trade_decision = decision
                        self.logger.info(
                            f"[CORE] Signal liquidity retenu sur {decision.get('asset')}"
                        )
                        print(
                            f"✅ [CORE] Décision liquidity détectée sur {decision.get('asset')}"
                        )
                except Exception as e:
                    self.logger.error(f"[CORE] Erreur evaluate_entry liquidity: {e}")

        # --- Aucun signal ---
        if not trade_decision:
            self.logger.info("[CORE] Aucun signal exploitable (scalping/liquidity)")
            print("⚠️ [CORE] Aucun trade décidé ce cycle.")
            return {}

        # --- 🔒 Normalisation/Validation ACTION & ASSET (anti-UNKNOWN) ---
        action_raw = str(trade_decision.get("action", "")).upper()
        action_map = {
            "LONG": "BUY",
            "SHORT": "SELL",
            "BUY": "BUY",
            "SELL": "SELL",
            "CLOSE": "CLOSE",
        }
        normalized_action = action_map.get(action_raw)

        if not normalized_action:
            self.logger.warning(
                f"Action inconnue '{action_raw}' depuis core_evaluate_signals -> décision ignorée proprement."
            )
            print(f"⚠️ [CORE] Action inconnue '{action_raw}' → décision ignorée.")
            self.config_manager.log_decision(
                current_config,
                {},
                context,
                f"Décision ignorée (action inconnue: {action_raw})",
            )
            return {}

        asset_raw = str(trade_decision.get("asset", "")).upper().strip()
        if not asset_raw:
            self.logger.warning("Décision reçue sans 'asset' -> décision ignorée.")
            print("⚠️ [CORE] Décision sans 'asset' → ignorée.")
            self.config_manager.log_decision(
                current_config, {}, context, "Décision ignorée (asset vide)."
            )
            return {}

        allowed_assets = set(map(str.upper, current_config.get("tradeable_assets", [])))
        if allowed_assets and asset_raw not in allowed_assets:
            self.logger.warning(
                f"Asset '{asset_raw}' non autorisé pour la stratégie '{strategy_name}'. Whitelist: {sorted(allowed_assets)}"
            )
            print(
                f"⚠️ [CORE] Asset '{asset_raw}' non autorisé pour '{strategy_name}' → ignoré."
            )
            self.config_manager.log_decision(
                current_config,
                {},
                context,
                f"Décision ignorée (asset non autorisé: {asset_raw})",
            )
            return {}

        order_type = str(trade_decision.get("order_type", "MARKET")).upper()
        if order_type not in {
            "MARKET",
            "BUY_LIMIT",
            "SELL_LIMIT",
            "BUY_STOP",
            "SELL_STOP",
        }:
            self.logger.debug(f"order_type inconnu '{order_type}', fallback 'MARKET'.")
            order_type = "MARKET"

        trade_decision["action"] = normalized_action
        trade_decision["asset"] = asset_raw
        trade_decision["order_type"] = order_type
        print(
            f"📝 [CORE] Décision normalisée → {normalized_action} {asset_raw} | type={order_type}"
        )

        # ==========================================================
        # 📊 Analyse patterns / bougies (Desk Pro Mode)
        # ==========================================================
        try:
            md_asset = (context.get("market_data", {}) or {}).get(asset_raw, {}) or {}
            df_patterns = md_asset.get("annotated_rates_df") or md_asset.get("rates_df")

            if isinstance(df_patterns, pd.DataFrame) and not df_patterns.empty:
                analysis = self.pattern_engine.analyze(df_patterns)
                last_sig = self.pattern_engine.latest_signal(df_patterns)

                context.setdefault("pattern_analysis", {})[asset_raw] = {
                    "all_patterns": analysis,
                    "latest_signal": last_sig,
                }

                if last_sig and last_sig.get("pattern") in {
                    "bullish_engulfing",
                    "morning_star",
                    "hammer",
                    "bearish_engulfing",
                    "evening_star",
                    "shooting_star",
                }:
                    trade_decision["rule_name"] = (
                        trade_decision.get("rule_name", "") + "+pattern"
                    )
                    trade_decision["confidence"] = min(
                        1.0, float(trade_decision.get("confidence", 0.5)) + 0.3
                    )
                    print(
                        f"🕯️ [CORE] Pattern fort reconnu → {last_sig['pattern']} (confiance boostée)"
                    )
        except Exception as e:
            self.logger.warning(f"Erreur PatternEngine: {e}")

        # ==========================================================
        # ✅ CONTRÔLE LIMITES DE TRADES (dynamique depuis config)
        # ==========================================================
        rm_cfg = current_config.get("risk_management") or {}
        max_trades_total = int(rm_cfg.get("max_trades_per_day", 999))
        max_trades_asset = int(rm_cfg.get("max_trades_per_asset_per_day", 999))

        trades_today = int(context.get("daily_trade_count", 0))
        trades_for_asset_today = int(
            (context.get("trades_by_asset", {}) or {}).get(asset_raw, 0)
        )

        if trades_today >= max_trades_total:
            msg = f"🚫 Trade bloqué: limite journalière {max_trades_total} atteinte."
            self.logger.warning(msg)
            print(f"⛔ [CORE] {msg}")
            return {}

        if trades_for_asset_today >= max_trades_asset:
            msg = (
                f"🚫 Trade bloqué: limite {max_trades_asset} atteinte pour {asset_raw}."
            )
            self.logger.warning(msg)
            print(f"⛔ [CORE] {msg}")
            return {}

        # --- Prix courant (commun à tous les modules) ---
        def _num(v, default=np.nan):
            try:
                x = float(v)
                return x if math.isfinite(x) else default
            except Exception:
                return default

        price = None
        for key in ("entry_price", "current_price", "last_close", "close"):
            if key in trade_decision and trade_decision.get(key) is not None:
                price = trade_decision.get(key)
                break
            if (
                isinstance(signals.get("M1"), dict)
                and signals["M1"].get(key) is not None
            ):
                price = signals["M1"].get(key)
                break
            if signals.get(key) is not None:
                price = signals.get(key)
                break
        price = _num(price)

        # 👉 Assurer l'entry pour le risk engine
        if isinstance(price, float) and math.isfinite(price):
            trade_decision["entry_price"] = float(price)
        else:
            self.logger.warning(
                "Aucun entry_price détecté → risque de risk_calc_failed."
            )
            print("⚠️ [CORE] entry_price manquant — le risk engine risque d'échouer.")

        # ==========================================================
        # ➕ Option EMA/RSI/ATR Trailing — ENTRÉES UNIQUEMENT
        # ==========================================================
        used_ema_decision = False
        if (
            strategy_name.lower() == "scalping"
            and current_config.get("use_ema_rsi_atr_trail", True)
            and isinstance(price, float)
            and math.isfinite(price)
        ):
            md = (context.get("market_data", {}) or {}).get(asset_raw, {}) or {}

            # ⚠️ Anti-ambiguïté pandas
            df_ema = md.get("annotated_rates_df")
            if not isinstance(df_ema, pd.DataFrame) or df_ema.empty:
                df_ema = md.get("rates_df")
            if not isinstance(df_ema, pd.DataFrame) or df_ema.empty:
                df_ema = None

            if isinstance(df_ema, pd.DataFrame) and not df_ema.empty:
                account_equity = float(
                    (context.get("account_info", {}) or {}).get("equity", 0.0) or 0.0
                )
                ema_dec = self._build_decision_ema_rsi_atr_trail(
                    asset_raw,
                    df_ema,
                    price,
                    account_equity,
                    context.get("current_position"),
                    current_config,
                )
                act = str((ema_dec or {}).get("action", "")).upper()
                if act in {"BUY", "SELL"}:
                    try:
                        sl_price = float(ema_dec["stop_loss"])
                    except Exception:
                        sl_price = float("nan")

                    if math.isfinite(sl_price):
                        min_rr_local = float(
                            (
                                (current_config.get("risk_management", {}) or {}).get(
                                    "min_rr", 1.5
                                )
                            )
                            or 1.5
                        )
                        sl_dist = abs(price - sl_price)
                        tp_price = (
                            price + min_rr_local * sl_dist
                            if act == "BUY"
                            else price - min_rr_local * sl_dist
                        )

                        trade_decision = {
                            "action": act,
                            "asset": asset_raw,
                            "order_type": "MARKET",
                            "entry_price": price,
                            "sl_price": float(round(sl_price, 10)),
                            "tp_price": float(round(tp_price, 10)),
                            "rule_name": "ema_rsi_atr_trail",
                            "level_mode": "ema_rsi_atr",
                        }
                        used_ema_decision = True
                        print(
                            f"✅ [CORE] Entrée EMA/RSI/ATR → {act} {asset_raw} (SL={sl_price}, TP={tp_price})"
                        )
                    else:
                        self.logger.info(
                            "EMA/RSI/ATR: SL invalide -> on ignore l'entrée EMA et on continue."
                        )
                elif act in {"EXIT_LONG", "EXIT_SHORT", "UPDATE_TRAIL"}:
                    self.logger.debug(
                        "EMA/RSI/ATR: action de gestion de position détectée (ignorée ici)."
                    )

            # ➕ Gate "Big Reversal Candle"
            if not used_ema_decision:
                try:
                    df_rev = md.get("annotated_rates_df")
                    if isinstance(df_rev, pd.DataFrame) and not df_rev.empty:
                        from phase_observer.detectors import Detectors

                        det = Detectors()
                        rev_signals = det.detect_big_reversal_candle(df_rev)
                        last_signal = rev_signals[-1] if rev_signals else None
                        if last_signal and last_signal.get("type"):
                            trade_decision.update(
                                {
                                    "rule_name": "big_reversal_candle",
                                    "level_mode": "big_reversal",
                                    "big_reversal": last_signal,
                                }
                            )
                            self.logger.info(
                                f"🎯 Signal Big Reversal validé ({last_signal['type']}) pour {asset_raw}"
                            )
                            print(
                                f"🔎 [CORE] Confluence Big Reversal reconnue ({last_signal['type']})."
                            )
                except Exception as e:
                    self.logger.debug(f"Erreur gate Big Reversal Candle: {e}")

                # ==========================================================
                # ✅ MODE BURST SCALPING
                # ==========================================================
                burst_cfg = current_config.get("burst_scalping", {}) or {}
                burst_enabled = bool(burst_cfg.get("enabled", True))
                asset_sig = signals.get(asset_raw, {})

                if burst_enabled and asset_sig.get("burst_signal"):
                    burst_side = str(asset_sig.get("burst_side", "NEUTRAL")).upper()
                    if burst_side in {"BUY", "SELL"}:
                        burst_size = int(
                            asset_sig.get("suggested_burst_size")
                            or burst_cfg.get("burst_size", 3)
                        )

                        # pip_size
                        si = (
                            context.get("market_data", {}).get(asset_raw, {}) or {}
                        ).get("symbol_info", {}) or {}
                        point = float(si.get("point") or signals.get("point") or 0.0001)
                        digits = int(si.get("digits") or 5)
                        pip_points = 10.0 if digits in (3, 5) else 1.0
                        pip_size = point * pip_points

                        # SL basé sur config
                        sl_pips = float(
                            asset_sig.get("burst_sl_pips")
                            or burst_cfg.get("sl_pips", 5.0)
                        )
                        sl_price = (
                            price - (sl_pips * pip_size)
                            if burst_side == "BUY"
                            else price + (sl_pips * pip_size)
                        )

                        # Trailing Stop Config
                        tp_sl_cfg = (burst_cfg.get("tp_sl") or {}).get("trailing", {})
                        trailing_enabled = bool(tp_sl_cfg.get("enabled", True))
                        trigger_pips = float(tp_sl_cfg.get("trigger_pips", 15))
                        step_pips = float(tp_sl_cfg.get("step_pips", 5))
                        activate_after_rr = float(
                            tp_sl_cfg.get("activate_after_rr", trigger_pips)
                        )

                        basket_id = f"burst_{asset_raw}_{int(time.time())}"

                        burst_decisions = []
                        for i in range(burst_size):
                            order = {
                                "action": burst_side,
                                "asset": asset_raw,
                                "order_type": "MARKET",
                                "entry_price": price,
                                "sl_price": round(sl_price, digits),
                                "rule_name": "burst_scalping",
                                "basket_id": basket_id,
                                "burst_index": i + 1,
                                "burst_size": burst_size,
                            }

                            if trailing_enabled:
                                order["trailing"] = {
                                    "enabled": True,
                                    "activate_after_rr": activate_after_rr,
                                    "step_pips": step_pips,
                                }

                            burst_decisions.append(order)

                        self.logger.info(
                            f"🔥 Burst Scalping activé: {burst_size} ordres {burst_side} sur {asset_raw} "
                            f"avec Trailing Stop (trigger={trigger_pips}p, step={step_pips}p)."
                        )
                        print(
                            f"🔥 [CORE] Burst Scalping → {burst_size}x {burst_side} {asset_raw} "
                            f"(SL={sl_price}, Trailing Stop: trigger={trigger_pips}p, step={step_pips}p)"
                        )

                        # === Final Decision Burst ===
                        final_decision = {
                            "action": burst_side,
                            "asset": asset_raw,
                            "order_type": "MARKET",
                            "entry_price": price,
                            "sl_price": round(sl_price, digits),
                            "rule_name": "burst_scalping",
                            "burst_enabled": True,
                            "burst_size": burst_size,
                        }
                        if trailing_enabled:
                            final_decision["trailing"] = {
                                "enabled": True,
                                "activate_after_rr": activate_after_rr,
                                "step_pips": step_pips,
                            }

                        return {
                            "final_decision": final_decision,
                            "config_used": current_config,
                            "burst_decisions": burst_decisions,
                        }


                # ==========================================================
                # ✅ Liquidity Sweep (optionnel)
                # ==========================================================
                if strategy_name.lower() == "scalping":
                    sweep_cfg = (current_config.get("scalping") or {}).get(
                        "liquidity_sweep", {}
                    ) or {}
                    lookback_bars = int(sweep_cfg.get("lookback_bars", 20))

                    md2 = (context.get("market_data", {}) or {}).get(
                        asset_raw, {}
                    ) or {}
                    df_ls = md2.get("annotated_rates_df")
                if isinstance(df_ls, pd.DataFrame) and len(df_ls) >= lookback_bars:
                    recent_high = df_ls["high"].tail(lookback_bars).max()
                    recent_low = df_ls["low"].tail(lookback_bars).min()

                    if price >= recent_high:
                        trade_decision = {
                            "action": "SELL",
                            "asset": asset_raw,
                            "order_type": "MARKET",
                            "entry_price": price,
                            "rule_name": "liquidity_sweep_high",
                            "level_mode": "sweep",
                        }
                        print("💧 [CORE] Liquidity sweep HIGH → SELL.")
                    elif price <= recent_low:
                        trade_decision = {
                            "action": "BUY",
                            "asset": asset_raw,
                            "order_type": "MARKET",
                            "entry_price": price,
                            "rule_name": "liquidity_sweep_low",
                            "level_mode": "sweep",
                        }
                        print("💧 [CORE] Liquidity sweep LOW → BUY.")


        # 4) Contrôles compte/risque simples
        active_broker_account = context.get("active_broker_account", {})
        max_positions_for_account = active_broker_account.get("trade_settings", {}).get(
            "max_open_positions", 999
        )
        current_open_positions = context.get("open_positions", [])

        self.logger.debug(
            f"Positions ouvertes actuelles: {len(current_open_positions)} / Max: {max_positions_for_account}"
        )
        if len(current_open_positions) >= max_positions_for_account:
            msg = f"Trade bloqué: Max positions ({max_positions_for_account}) atteint pour le compte {active_broker_account.get('account_id')}."
            self.logger.warning(msg)
            print(f"⛔ [CORE] {msg}")
            return {}

        # 5) Sizing au risque
        risk_params = self.calculate_risk_parameters(
            context, current_config, trade_decision
        )
        self.logger.debug(f"Paramètres de risque calculés: {risk_params}")

        if isinstance(risk_params, dict) and risk_params.get("ok"):
            vol_ok = risk_params.get("volume")
            if isinstance(vol_ok, (int, float)) and vol_ok > 0:
                trade_decision["volume"] = float(vol_ok)
            if risk_params.get("sl_price") is not None:
                trade_decision["sl_price"] = float(risk_params["sl_price"])

           # ✅ Patch : pas de TP pour burst
            if trade_decision.get("rule_name") == "burst_scalping":
                levels = normalize_levels(
                    entry_price=trade_decision.get("entry_price"),
                    action=trade_decision.get("action"),
                    pip_size=pip_size,
                    sl_pips=trade_decision.get("target_sl_pips"),
                    sl_price=trade_decision.get("sl_price"),
                    # 🚫 pas de tp_pips ni tp_price
                )
                trade_decision["sl_price"] = levels["sl"]
                # volontairement pas de TP
            else:
                levels = normalize_levels(
                    entry_price=trade_decision.get("entry_price"),
                    action=trade_decision.get("action"),
                    pip_size=pip_size,
                    sl_pips=trade_decision.get("target_sl_pips"),
                    tp_pips=trade_decision.get("target_tp_pips"),
                    sl_price=trade_decision.get("sl_price"),
                    tp_price=trade_decision.get("tp_price"),
                )
                trade_decision["sl_price"] = levels["sl"]
                trade_decision["tp_price"] = levels["tp"]


                # plancher volume soft
                try:
                    min_lot_cfg = float(current_config.get("min_lot_size", 0.01))
                    md_asset = (context.get("market_data", {}) or {}).get(
                        trade_decision["asset"], {}
                    ) or {}
                    si = (
                        md_asset.get("symbol_info")
                        or current_config.get("symbol_info")
                        or {}
                    )

                    def _g(d, k, default=0.0):
                        try:
                            v = d.get(k) if isinstance(d, dict) else getattr(d, k, None)
                            v = float(v) if v is not None else default
                            return v if math.isfinite(v) else default
                        except Exception:
                            return default

                    vol_min_broker = _g(si, "volume_min", 0.0)
                    vol_step_broker = _g(si, "volume_step", 0.0)
                    min_lot_soft = max(
                        min_lot_cfg, vol_min_broker if vol_min_broker > 0 else 0.0
                    )
                    vol = float(trade_decision.get("volume", 0.0))
                    vol = max(vol, min_lot_soft) if vol > 0 else min_lot_soft
                    if vol_step_broker and vol_step_broker > 0:
                        steps = math.ceil(vol / vol_step_broker)
                        vol = steps * vol_step_broker
                    trade_decision["volume"] = float(vol)
                    self.logger.info(
                        f"[SOFT-ATR] Volume relevé au plancher soft: {trade_decision['volume']} (min {min_lot_soft}, step {vol_step_broker or 'n/a'})"
                    )
                except Exception as e:
                    self.logger.debug(f"[SOFT-ATR] Ajustement volume ignoré: {e}")

            # plancher de volume en cas de soft ATR
            try:
                flags = trade_decision.get("flags", {})
                if flags.get("soft_atr_m1_low") and trade_decision.get("action") in {
                    "BUY",
                    "SELL",
                }:
                    min_lot_cfg = float(current_config.get("min_lot_size", 0.01))
                    md_asset = (context.get("market_data", {}) or {}).get(
                        asset_raw, {}
                    ) or {}
                    si = (
                        md_asset.get("symbol_info")
                        or current_config.get("symbol_info")
                        or {}
                    )

                    def _get_num(d, k, default=0.0):
                        try:
                            v = d.get(k) if isinstance(d, dict) else getattr(d, k, None)
                            v = float(v) if v is not None else default
                            return v if math.isfinite(v) else default
                        except Exception:
                            return default

                    vol_min_broker = _get_num(si, "volume_min", 0.0)
                    vol_step_broker = _get_num(si, "volume_step", 0.0)
                    min_lot_soft = max(
                        min_lot_cfg, vol_min_broker if vol_min_broker > 0 else 0.0
                    )
                    vol = float(trade_decision.get("volume", 0.0))
                    if vol <= 0.0:
                        vol = min_lot_soft
                    else:
                        vol = max(vol, min_lot_soft)
                    if vol_step_broker and vol_step_broker > 0:
                        steps = math.ceil(vol / vol_step_broker)
                        vol = steps * vol_step_broker
                    trade_decision["volume"] = float(vol)
                    self.logger.info(
                        f"[SOFT-ATR] Volume relevé au plancher soft: {trade_decision['volume']} (min {min_lot_soft}, step {vol_step_broker or 'n/a'})"
                    )
            except Exception as e:
                self.logger.debug(f"[SOFT-ATR] Patch plancher de volume ignoré: {e}")

        # === RÈGLE 2 : Trailing Stop
        try:
            if normalized_action in {"BUY", "SELL"}:
                trail_cfg = (current_config.get("scalping") or {}).get(
                    "trailing_stop", {}
                ) or {}
                enable_trail = bool(trail_cfg.get("enabled", True))
                trail_distance_pips = float(trail_cfg.get("distance_pips", 5.0))

                md_asset = (context.get("market_data", {}) or {}).get(
                    asset_raw, {}
                ) or {}
                si = (
                    md_asset.get("symbol_info")
                    or current_config.get("symbol_info")
                    or {}
                ) or {}
                point = float(si.get("point") or asset_sig.get("point") or 0.0001)
                digits = int(si.get("digits") or 5)
                pip_points = 10.0 if digits in (3, 5) else 1.0
                pip_size = point * pip_points

                if (
                    enable_trail
                    and isinstance(price, float)
                    and math.isfinite(price)
                    and pip_size > 0
                ):
                    if normalized_action == "BUY":
                        trade_decision["trailing_stop"] = price - (
                            trail_distance_pips * pip_size
                        )
                    else:
                        trade_decision["trailing_stop"] = price + (
                            trail_distance_pips * pip_size
                        )

                    trade_decision["rule_name"] = (
                        trade_decision.get("rule_name", "") + "+trailing"
                    )
                    self.logger.info(
                        f"Trailing Stop appliqué ({trail_distance_pips} pips) pour {asset_raw}"
                    )
                    print(f"🪢 [CORE] Trailing appliqué ({trail_distance_pips} pips).")
        except Exception as e:
            self.logger.warning(f"Erreur application Trailing Stop: {e}")
            print(f"⚠️ [CORE] Erreur trailing: {e}")
            
        # ✅ Patch Burst Scalping : suppression physique du TP
        if trade_decision.get("rule_name") == "burst_scalping":
            levels = normalize_levels(
                entry_price=trade_decision.get("entry_price"),
                action=trade_decision.get("action"),
                pip_size=pip_size,
                sl_pips=trade_decision.get("target_sl_pips"),
                sl_price=trade_decision.get("sl_price"),
                # 🚫 pas de tp_pips ni tp_price
            )
            trade_decision["sl_price"] = levels["sl"]
            # pas de ligne trade_decision["tp_price"]
        else:
            levels = normalize_levels(
                entry_price=trade_decision.get("entry_price"),
                action=trade_decision.get("action"),
                pip_size=pip_size,
                sl_pips=trade_decision.get("target_sl_pips"),
                tp_pips=trade_decision.get("target_tp_pips"),
                sl_price=trade_decision.get("sl_price"),
                tp_price=trade_decision.get("tp_price"),
            )
            trade_decision["sl_price"] = levels["sl"]
            trade_decision["tp_price"] = levels["tp"]
                      
        self.logger.debug(
            f"[CORE] Niveaux normalisés pour {trade_decision['asset']} → SL={levels['sl']} | TP={levels['tp']}"
        )

        # Log final (décision avant exécution)
        self.config_manager.log_decision(
            current_config,
            trade_decision,
            context,
            f"Décision CORE avec paramètres '{strategy_name}': {trade_decision.get('rule_name', 'N/A')}",
        )
        print(
            f"📦 [CORE] Décision finale prête → {trade_decision.get('action','?')} "
            f"{trade_decision.get('asset','?')} | vol={trade_decision.get('volume','?')} | "
            f"SL={trade_decision.get('sl_price','?')} | TP={trade_decision.get('tp_price','?')}"
        )

        # ==========================================================
        # 📋 Log final enrichi avec analyse patterns (si dispo)
        # ==========================================================
        try:
            latest_pattern = (
                context.get("pattern_analysis", {})
                .get(asset_raw, {})
                .get("latest_signal")
            )
            if latest_pattern:
                self.logger.info(
                    f"[PATTERN] Dernier signal {asset_raw}: {latest_pattern.get('pattern')} "
                    f"(type={latest_pattern.get('signal_type')}, bullish={latest_pattern.get('is_bullish')})"
                )
                print(
                    f"🕯️ [PATTERN] {asset_raw} → {latest_pattern.get('pattern')} "
                    f"(type={latest_pattern.get('signal_type')}, bullish={latest_pattern.get('is_bullish')})"
                )
        except Exception as e:
            self.logger.debug(f"[PATTERN] Log final ignoré: {e}")

        # [EXEC-01] Exécution immédiate : envoi au TradeExecutor (pas de dry-run)
        try:
            from trader.trade_executor import (
                TradeExecutor,
                run_trade_execution_pipeline,
            )
        except Exception as e:
            self.logger.exception(f"[EXECUTOR] Import trade_executor impossible: {e}")
            self.logger.error(
                "[EXECUTOR] Import échoué → exécution réelle impossible. Aucune voie de simulation n'est autorisée."
            )
            print("⛔ [EXECUTOR] Import trade_executor impossible — exécution annulée.")
            return {}

        try:
            # 2) essayer de récupérer un exécuteur déjà prêt sur self
            te = getattr(self, "trade_executor", None)

            # 3) si absent, essayer de récupérer/instancier un MT5Connector existant
            mt5c = getattr(self, "mt5_connector", None) or context.get("mt5_connector")

            # 3a) fallback: créer le connecteur si toujours None (MT5Connector est dans le fichier général)
            if mt5c is None:
                try:
                    from mt5_connector import (
                        MT5Connector,
                    )  # <- import unique depuis le fichier général

                    mt5c = MT5Connector()  # __init__ sans argument

                    # récupérer des identifiants valides
                    account_id = (
                        (context.get("active_broker_account", {}) or {}).get(
                            "account_id"
                        )
                        or current_config.get("mt5_account_id")
                        or "main_demo_broker_A"
                    )
                    run_mode = (
                        context.get("run_mode") or current_config.get("mode") or "DEMO"
                    )

                    try:
                        creds = self.config_manager.get_mt5_account_credentials(
                            account_id=account_id, mode=run_mode
                        )
                    except Exception:
                        creds = (
                            (context.get("active_broker_account", {}) or {}).get(
                                "credentials"
                            )
                        ) or {}

                    ok = False
                    if isinstance(creds, dict) and creds and hasattr(mt5c, "connect"):
                        try:
                            ok = bool(mt5c.connect(creds))
                        except Exception as ce:
                            self.logger.error(
                                f"[EXECUTOR] Échec connect() MT5Connector: {ce}"
                            )

                    if ok:
                        setattr(self, "mt5_connector", mt5c)
                        try:
                            context["mt5_connector"] = mt5c
                        except Exception:
                            pass
                        self.logger.info(
                            "[EXECUTOR] MT5Connector initialisé et connecté."
                        )
                        print("🔌 [EXECUTOR] MT5Connector connecté.")
                    else:
                        self.logger.error(
                            "[EXECUTOR] Connexion MT5 impossible — envoi BLOQUÉ."
                        )
                        print("⛔ [EXECUTOR] Connexion MT5 impossible — envoi BLOQUÉ.")
                        mt5c = None

                except Exception as e:
                    self.logger.error(
                        f"[EXECUTOR] Impossible d'obtenir un MT5Connector: {e}"
                    )
                    print(f"⛔ [EXECUTOR] Création MT5Connector échouée: {e}")
                    mt5c = None

            # 3bis) S'assurer que le connecteur est connecté si dispo
            try:
                is_conn = getattr(mt5c, "is_connected", False)
                if callable(is_conn):
                    is_conn = is_conn()
                if not is_conn and hasattr(mt5c, "connect"):
                    creds_ctx = (
                        (context.get("active_broker_account", {}) or {}).get(
                            "credentials"
                        )
                    ) or {}
                    if creds_ctx:
                        mt5c.connect(creds_ctx)
            except Exception:
                pass  # l'exécuteur gèrera l'erreur de connexion

            # 4) si pas d'exécuteur mais on a un connecteur, on instancie proprement
            if te is None and mt5c is not None:
                te = TradeExecutor(self.config_manager, mt5c)
                setattr(self, "trade_executor", te)

            if te is not None:

                # === Gestion Multi-TP ===
                tp_prices = trade_decision.get("tp_prices")
                if isinstance(tp_prices, list) and len(tp_prices) > 1:
                    try:
                        requests = te._split_multi_tp_orders(
                            trade_decision,
                            current_config,
                            trade_decision["volume"],
                            trade_decision["entry_price"],
                            trade_decision["sl_price"],
                            tp_prices,
                            context.get("market_data", {})
                            .get(asset_raw, {})
                            .get("symbol_info", {}),
                        )
                        trade_decision["multi_tp_requests"] = requests
                        self.logger.info(
                            f"Multi-TP activé: {len(requests)} ordres générés."
                        )
                    except Exception as e:
                        self.logger.error(f"Erreur génération Multi-TP: {e}")

                decision_package = {
                    "final_decision": trade_decision,  # ⚠️ clé attendue par run_trade_execution_pipeline
                    "config_used": current_config,
                    "context": context,
                }

                print(
                    f"🚀 [EXECUTOR] Envoi ordre → {trade_decision.get('action')} {trade_decision.get('asset')} "
                    f"| vol={trade_decision.get('volume')} | SL={trade_decision.get('sl_price')} | TP={trade_decision.get('tp_price')}"
                )
                # --- PATCH: Construction et exécution directe via TradeExecutor ---
                try:
                    md_asset = (context.get("market_data", {}) or {}).get(
                        asset_raw, {}
                    ) or {}
                    si = (
                        md_asset.get("symbol_info")
                        or current_config.get("symbol_info")
                        or {}
                    )

                    point = float(si.get("point") or 0.0001)
                    digits = int(si.get("digits") or 5)
                    pip_points = 10.0 if digits in (3, 5) else 1.0
                    pip_size = point * pip_points

                    req = {
                        "symbol": trade_decision["asset"],
                        "type": getattr(
                            te.mt5, f"ORDER_TYPE_{trade_decision['action']}"
                        ),
                        "volume": trade_decision.get("volume", 0.1),
                        "price": trade_decision.get("entry_price"),
                        "sl": trade_decision.get("sl_price"),
                        "tp": trade_decision.get("tp_price"),
                        "deviation": current_config.get("max_slippage_points", 20),
                        "magic": current_config.get("magic_number", 123456),
                        "comment": trade_decision.get("rule_name", "core_decision"),
                        "strategy_type": trade_decision.get("strategy_type", "core"),
                        "rule_name": trade_decision.get("rule_name", "core"),
                        "pip_size": pip_size,
                    }

                    print(
                        f"🚀 [EXECUTOR-PATCH] Envoi direct ordre → {req['symbol']} | {req['type']} "
                        f"| vol={req['volume']} | SL={req['sl']} | TP={req['tp']}"
                    )

                    exec_res = te.execute_order(req)
                    self.logger.info(
                        f"[EXECUTOR-PATCH] Résultat exécution directe: {exec_res}"
                    )

                    # enrichir la décision avec le résultat
                    trade_decision["execution_status"] = exec_res.get(
                        "status", "unknown"
                    )
                    trade_decision["executed"] = trade_decision["execution_status"] in {
                        "filled",
                        "placed",
                    }
                    trade_decision["order_id"] = exec_res.get("order")
                    trade_decision["deal_id"] = exec_res.get("deal")
                    trade_decision["execution_price"] = exec_res.get("price")

                    # logs humains
                    if trade_decision["executed"]:
                        print(
                            f"🎉 [EXECUTOR-PATCH] TRADE EXÉCUTÉ → {trade_decision['action']} {trade_decision['asset']} "
                            f"@{trade_decision['execution_price']} (vol={trade_decision['volume']})"
                        )
                    else:
                        print(
                            f"⚠️ [EXECUTOR-PATCH] Trade non exécuté: status={trade_decision['execution_status']}"
                        )

                except Exception as e:
                    self.logger.error(
                        f"[EXECUTOR-PATCH] Erreur envoi direct MT5: {e}", exc_info=True
                    )
                    print(f"💥 [EXECUTOR-PATCH] Erreur exécution: {e}")

                exec_res = run_trade_execution_pipeline(
                    te, decision_package, is_dry_run=False
                )
                self.logger.info(f"[EXECUTOR] Envoi MT5 terminé: {exec_res}")

                # --- Enrichir la décision avec le résultat d'exécution ---
                try:
                    status = str(exec_res.get("status", "")).lower()
                    trade_decision["execution_status"] = status
                    if status in {"filled", "placed"}:
                        trade_decision["executed"] = True
                        trade_decision["order_id"] = exec_res.get("order")
                        trade_decision["deal_id"] = exec_res.get("deal")
                        trade_decision["execution_price"] = exec_res.get("price")
                        # si le volume retourné est renseigné, on l’utilise (sinon on garde celui calculé)
                        if (
                            isinstance(exec_res.get("volume"), (int, float))
                            and exec_res["volume"] > 0
                        ):
                            trade_decision["volume"] = exec_res["volume"]
                    else:
                        trade_decision["executed"] = False
                except Exception:
                    pass

                # Affichage console clair du résultat API
                try:
                    status = str((exec_res or {}).get("status", "")).lower()
                    order = (exec_res or {}).get("order")
                    deal = (exec_res or {}).get("deal")
                    retcode = (exec_res or {}).get("retcode")
                    price_ex = (exec_res or {}).get("price")
                    vol_ex = (exec_res or {}).get("volume")

                    print(
                        f"✅ [EXECUTOR] Résultat MT5 → status={status} | order={order} | deal={deal} | retcode={retcode} | "
                        f"price={price_ex} | volume={vol_ex}"
                    )

                    trade_decision["execution_status"] = status
                    trade_decision["executed"] = status in {"filled", "placed"}
                    meta = trade_decision.setdefault("meta", {})
                    meta["execution_result"] = {
                        k: exec_res.get(k)
                        for k in (
                            "status",
                            "order",
                            "deal",
                            "retcode",
                            "comment",
                            "price",
                            "volume",
                        )
                        if isinstance(exec_res, dict) and k in exec_res
                    }

                    # LOG FINAL HUMAIN-READABLE
                    if trade_decision["executed"]:
                        print(
                            f"🎉 [EXECUTOR] TRADE EXÉCUTÉ → {trade_decision.get('action')} {trade_decision.get('asset')} "
                            f"@{price_ex} (vol={vol_ex}) | SL={trade_decision.get('sl_price')} | TP={trade_decision.get('tp_price')}"
                        )
                    else:
                        print(
                            "⚠️ [EXECUTOR] Trade non exécuté (status différent de filled/placed)."
                        )

                except Exception:
                    # même si l'affichage échoue, on ne casse pas la fonction
                    pass

            else:
                self.logger.error(
                    "[EXECUTOR] Pas d'Executor/MT5Connector → envoi BLOQUÉ (aucune simulation)."
                )
                print("⛔ [EXECUTOR] Pas d'Executor/MT5Connector — envoi annulé.")
                return {}

        except Exception as e:
            self.logger.exception(f"[EXECUTOR] Erreur d’exécution MT5: {e}")
            print(f"💥 [EXECUTOR] Erreur d’exécution: {e}")
            return {}

        # Toujours retourner la décision (enrichie du statut d’exécution)
        return trade_decision

    def _build_decision_ema_rsi_atr_trail(
        self,
        asset: str,
        df: pd.DataFrame,
        current_price: float,
        account_equity: float,
        current_position: dict | None,
        config: dict,
    ) -> dict:
        """
        Version intégrée de l’algo EMA/RSI/ATR trailing stop (scalping).
        - Utilise les params de config si dispo, sinon des défauts sûrs.
        - Retourne un package décisionnel enrichi pour le RiskEngine/Executor/Reporter.
        """

        # --- Paramètres (fallback depuis config) ---
        risk_pct = float(config.get("risk_per_trade_pct", 1.0))
        ema_short_period = int(config.get("ema_short_period", 5))
        ema_long_period = int(config.get("ema_long_period", 20))
        rsi_period = int(config.get("rsi_period", 14))
        rsi_overbought = float(config.get("rsi_overbought", 70))
        rsi_oversold = float(config.get("rsi_oversold", 30))
        atr_period = int(config.get("atr_period", 14))
        atr_mult_init = float(config.get("atr_multiplier_initial", 1.5))
        atr_mult_trail = float(config.get("atr_multiplier_trailing", 1.0))
        vol_th = float(config.get("volatility_filter_threshold", 0.5))  # en %
        rr_hint = float(config.get("rr_hint", 1.2))  # pour tp_pips_hint

        # --- Garde-fous basiques ---
        if not isinstance(df, pd.DataFrame) or df.empty:
            return {"action": "HOLD", "reason": "no_data"}
        need = max(ema_long_period, rsi_period, atr_period) + 1
        if len(df) < need:
            return {"action": "HOLD", "reason": f"insufficient_bars_{len(df)}/{need}"}
        try:
            cp = float(current_price)
            if not (cp > 0 and math.isfinite(cp)):
                return {"action": "HOLD", "reason": "invalid_current_price"}
        except Exception:
            return {"action": "HOLD", "reason": "invalid_current_price"}

        # --- Sanitize/numérisation colonnes ---
        df = df.copy()
        for c in ("open", "high", "low", "close", "volume"):
            if c not in df.columns:
                # colonnes minimales indispensables
                if c in ("high", "low", "close"):
                    return {"action": "HOLD", "reason": "missing_ohlc"}
                df[c] = np.nan
            df[c] = pd.to_numeric(df[c], errors="coerce")
        df.replace([np.inf, -np.inf], np.nan, inplace=True)
        df.dropna(subset=["high", "low", "close"], how="any", inplace=True)
        if len(df) < need:
            return {
                "action": "HOLD",
                "reason": f"insufficient_bars_postclean_{len(df)}/{need}",
            }

        # --- EMA ---
        df["ema_short"] = (
            df["close"]
            .ewm(span=ema_short_period, adjust=False, min_periods=ema_short_period)
            .mean()
        )
        df["ema_long"] = (
            df["close"]
            .ewm(span=ema_long_period, adjust=False, min_periods=ema_long_period)
            .mean()
        )

        # --- RSI (simple) ---
        delta = df["close"].diff()
        gain = delta.clip(lower=0).rolling(rsi_period, min_periods=rsi_period).mean()
        loss = (-delta.clip(upper=0)).rolling(rsi_period, min_periods=rsi_period).mean()
        rs = gain / loss.replace(0, np.nan)
        df["rsi"] = (100 - (100 / (1 + rs))).clip(lower=0, upper=100)

        # --- ATR (True Range) ---
        hc = (df["high"] - df["close"].shift()).abs()
        lc = (df["low"] - df["close"].shift()).abs()
        tr = pd.concat([(df["high"] - df["low"]).abs(), hc, lc], axis=1).max(axis=1)
        df["atr"] = tr.rolling(window=atr_period, min_periods=atr_period).mean()

        # --- Dernières valeurs ---
        try:
            ema_short_cur = float(df["ema_short"].iloc[-1])
            ema_long_cur = float(df["ema_long"].iloc[-1])
            ema_short_prev = float(df["ema_short"].iloc[-2])
            ema_long_prev = float(df["ema_long"].iloc[-2])
            rsi_cur = float(df["rsi"].iloc[-1])
            atr_cur = float(df["atr"].iloc[-1])
        except Exception:
            return {"action": "HOLD", "reason": "indicator_nan"}

        if not (math.isfinite(atr_cur) and atr_cur > 0):
            return {"action": "HOLD", "reason": "atr_unavailable"}

        # --- Volatility filter ---
        volatility_pct = (atr_cur / cp) * 100.0
        if volatility_pct < vol_th:
            decision = {
                "action": "HOLD",
                "reason": f"low_volatility_{volatility_pct:.2f}%<{vol_th:.2f}%",
            }
            decision.update(
                {
                    "rule_name": "ema_rsi_atr_trail",
                    "level_mode": "scalping",
                    "asset": asset,
                    "atr_pips": atr_cur,  # en unités de prix; si tu veux en pips -> convertir via pip_size
                    "ema_short": ema_short_cur,
                    "ema_long": ema_long_cur,
                    "rsi": rsi_cur,
                }
            )
            return decision

        # --- pip_size (si 'point' présent) pour fournir des hints pips ---
        pip_size = None
        try:
            point_col = float(df["point"].iloc[-1]) if "point" in df.columns else 0.0
            pip_size = point_col * 10.0 if point_col > 0 else None
        except Exception:
            pip_size = None

        # ========= Gestion d'une position existante =========
        if current_position:
            pos_type = str(current_position.get("type", "")).lower()
            trail = (
                float(current_position.get("trailing_stop", cp))
                if current_position.get("trailing_stop") is not None
                else cp
            )

            if pos_type == "long":
                new_trail = max(trail, cp - atr_cur * atr_mult_trail)

                # Exit par trailing touché
                if cp <= trail:
                    decision = {
                        "action": "EXIT_LONG",
                        "asset": asset,
                        "exit_price": cp,
                        "reason": "trailing_hit",
                    }
                    decision.update(
                        {
                            "rule_name": "ema_rsi_atr_trail",
                            "level_mode": "scalping",
                            "atr_pips": atr_cur,
                            "ema_short": ema_short_cur,
                            "ema_long": ema_long_cur,
                            "rsi": rsi_cur,
                        }
                    )
                    return decision

                # Exit par signal inverse fort
                if (ema_short_cur < ema_long_cur) and (rsi_cur > rsi_overbought):
                    decision = {
                        "action": "EXIT_LONG",
                        "asset": asset,
                        "exit_price": cp,
                        "reason": "inverse_signal",
                    }
                    decision.update(
                        {
                            "rule_name": "ema_rsi_atr_trail",
                            "level_mode": "scalping",
                            "atr_pips": atr_cur,
                            "ema_short": ema_short_cur,
                            "ema_long": ema_long_cur,
                            "rsi": rsi_cur,
                        }
                    )
                    return decision

                # Update trailing
                if new_trail > trail:
                    decision = {
                        "action": "UPDATE_TRAIL",
                        "asset": asset,
                        "trailing_stop": new_trail,
                        "reason": "adaptive_trail_update",
                    }
                    decision.update(
                        {
                            "rule_name": "ema_rsi_atr_trail",
                            "level_mode": "scalping",
                            "atr_pips": atr_cur,
                            "ema_short": ema_short_cur,
                            "ema_long": ema_long_cur,
                            "rsi": rsi_cur,
                        }
                    )
                    return decision

                decision = {
                    "action": "HOLD",
                    "asset": asset,
                    "reason": "position_active",
                }
                decision.update(
                    {
                        "rule_name": "ema_rsi_atr_trail",
                        "level_mode": "scalping",
                        "atr_pips": atr_cur,
                        "ema_short": ema_short_cur,
                        "ema_long": ema_long_cur,
                        "rsi": rsi_cur,
                    }
                )
                return decision

            elif pos_type == "short":
                new_trail = min(trail, cp + atr_cur * atr_mult_trail)

                if cp >= trail:
                    decision = {
                        "action": "EXIT_SHORT",
                        "asset": asset,
                        "exit_price": cp,
                        "reason": "trailing_hit",
                    }
                    decision.update(
                        {
                            "rule_name": "ema_rsi_atr_trail",
                            "level_mode": "scalping",
                            "atr_pips": atr_cur,
                            "ema_short": ema_short_cur,
                            "ema_long": ema_long_cur,
                            "rsi": rsi_cur,
                        }
                    )
                    return decision

                if (ema_short_cur > ema_long_cur) and (rsi_cur < rsi_oversold):
                    decision = {
                        "action": "EXIT_SHORT",
                        "asset": asset,
                        "exit_price": cp,
                        "reason": "inverse_signal",
                    }
                    decision.update(
                        {
                            "rule_name": "ema_rsi_atr_trail",
                            "level_mode": "scalping",
                            "atr_pips": atr_cur,
                            "ema_short": ema_short_cur,
                            "ema_long": ema_long_cur,
                            "rsi": rsi_cur,
                        }
                    )
                    return decision

                if new_trail < trail:
                    decision = {
                        "action": "UPDATE_TRAIL",
                        "asset": asset,
                        "trailing_stop": new_trail,
                        "reason": "adaptive_trail_update",
                    }
                    decision.update(
                        {
                            "rule_name": "ema_rsi_atr_trail",
                            "level_mode": "scalping",
                            "atr_pips": atr_cur,
                            "ema_short": ema_short_cur,
                            "ema_long": ema_long_cur,
                            "rsi": rsi_cur,
                        }
                    )
                    return decision

                decision = {
                    "action": "HOLD",
                    "asset": asset,
                    "reason": "position_active",
                }
                decision.update(
                    {
                        "rule_name": "ema_rsi_atr_trail",
                        "level_mode": "scalping",
                        "atr_pips": atr_cur,
                        "ema_short": ema_short_cur,
                        "ema_long": ema_long_cur,
                        "rsi": rsi_cur,
                    }
                )
                return decision

            # type inconnu
            decision = {
                "action": "HOLD",
                "asset": asset,
                "reason": "unknown_position_type",
            }
            decision.update(
                {
                    "rule_name": "ema_rsi_atr_trail",
                    "level_mode": "scalping",
                    "atr_pips": atr_cur,
                    "ema_short": ema_short_cur,
                    "ema_long": ema_long_cur,
                    "rsi": rsi_cur,
                }
            )
            return decision

        # ========= Pas de position : signaux d'entrée =========
        risk_amount = float(account_equity) * (risk_pct / 100.0)

        # LONG
        if (
            (ema_short_prev <= ema_long_prev)
            and (ema_short_cur > ema_long_cur)
            and (rsi_cur < rsi_overbought)
        ):
            stop_loss = cp - atr_cur * atr_mult_init
            sl_dist = max(cp - stop_loss, 1e-9)
            size = risk_amount / sl_dist

            decision = {
                "action": "BUY",
                "asset": asset,
                "entry_price": cp,
                "stop_loss": stop_loss,
                "position_size": size,
                "type": "long",
                "reason": "ema_cross_rsi_filter",
            }

            # Hints en pips (si pip_size dispo)
            if pip_size and pip_size > 0:
                sl_pips = sl_dist / pip_size
                decision["sl_pips_hint"] = float(sl_pips)
                decision["tp_pips_hint"] = float(rr_hint * sl_pips)

            decision.update(
                {
                    "rule_name": "ema_rsi_atr_trail",
                    "level_mode": "scalping",
                    "atr_pips": atr_cur,
                    "ema_short": ema_short_cur,
                    "ema_long": ema_long_cur,
                    "rsi": rsi_cur,
                }
            )
            return decision

        # SHORT
        if (
            (ema_short_prev >= ema_long_prev)
            and (ema_short_cur < ema_long_cur)
            and (rsi_cur > rsi_oversold)
        ):
            stop_loss = cp + atr_cur * atr_mult_init
            sl_dist = max(stop_loss - cp, 1e-9)
            size = risk_amount / sl_dist

            decision = {
                "action": "SELL",
                "asset": asset,
                "entry_price": cp,
                "stop_loss": stop_loss,
                "position_size": size,
                "type": "short",
                "reason": "ema_cross_rsi_filter",
            }

            if pip_size and pip_size > 0:
                sl_pips = sl_dist / pip_size
                decision["sl_pips_hint"] = float(sl_pips)
                decision["tp_pips_hint"] = float(rr_hint * sl_pips)

            decision.update(
                {
                    "rule_name": "ema_rsi_atr_trail",
                    "level_mode": "scalping",
                    "atr_pips": atr_cur,
                    "ema_short": ema_short_cur,
                    "ema_long": ema_long_cur,
                    "rsi": rsi_cur,
                }
            )
            return decision

        # Rien à faire
        decision = {"action": "HOLD", "asset": asset, "reason": "no_signal"}
        decision.update(
            {
                "rule_name": "ema_rsi_atr_trail",
                "level_mode": "scalping",
                "atr_pips": atr_cur,
                "ema_short": ema_short_cur,
                "ema_long": ema_long_cur,
                "rsi": rsi_cur,
            }
        )
        return decision

    def _evaluate_rule(
        self, rule: Dict[str, Any], asset_signals: Dict[str, Any]
    ) -> bool:
        """
        Évalue si un ensemble de signaux d'actif satisfait les conditions d'une règle.
        Déplacée de ConfigManager.
        """
        conditions = rule.get("conditions", {})
        rule_name = rule.get("name", "Unnamed Rule")

        self.logger.debug(
            f"Évaluation de la règle '{rule_name}' avec signaux: {asset_signals}"
        )

        min_confidence = conditions.get("min_confidence", 0.0)
        current_confidence = asset_signals.get("confidence_score", 0.0)
        if current_confidence < min_confidence:
            self.logger.debug(
                f"  Règle '{rule_name}' échouée: Confiance {current_confidence} < seuil min {min_confidence}."
            )
            return False

        phase_must_contain = conditions.get("phase_must_contain", [])
        current_phase = asset_signals.get("phase", "")
        if phase_must_contain and not any(
            p in current_phase for p in phase_must_contain
        ):
            self.logger.debug(
                f"  Règle '{rule_name}' échouée: Phase '{current_phase}' ne contient pas les phases requises ({phase_must_contain})."
            )
            return False

        signal_must_contain = conditions.get("signal_must_contain", [])
        for signal in signal_must_contain:
            if signal == "fvg":
                if not asset_signals.get("fvg_detected", False):
                    self.logger.debug(
                        f"  Règle '{rule_name}' échouée: Signal 'fvg' non détecté."
                    )
                    return False
            elif signal == "order_block":
                if not asset_signals.get("ob_detected", False):
                    self.logger.debug(
                        f"  Règle '{rule_name}' échouée: Signal 'order_block' non détecté."
                    )
                    return False
            elif signal == "bos_mss":
                if not asset_signals.get("bos_mss_detected", False):
                    self.logger.debug(
                        f"  Règle '{rule_name}' échouée: Signal 'bos_mss' non détecté."
                    )
                    return False
            elif signal == "liquidity_grab":
                if not asset_signals.get("liquidity_grab_detected", False):
                    self.logger.debug(
                        f"  Règle '{rule_name}' échouée: Signal 'liquidity_grab' non détecté."
                    )
                    return False
            elif signal == "volume_anomaly_spike":
                if not (
                    asset_signals.get("volume_anomaly_details", {}).get("type")
                    == "spike"
                ):
                    self.logger.debug(
                        f"  Règle '{rule_name}' échouée: Signal 'volume_anomaly_spike' non détecté."
                    )
                    return False
            elif signal == "volume_anomaly_drought":
                if not (
                    asset_signals.get("volume_anomaly_details", {}).get("type")
                    == "drought"
                ):
                    self.logger.debug(
                        f"  Règle '{rule_name}' échouée: Signal 'volume_anomaly_drought' non détecté."
                    )
                    return False
            elif signal == "eqh_eql":
                if not asset_signals.get("eqh_eql_detected", False):
                    self.logger.debug(
                        f"  Règle '{rule_name}' échouée: Signal 'eqh_eql' non détecté."
                    )
                    return False
            elif signal == "entry_confirmation_bullish":
                if not asset_signals.get("entry_confirmation_bullish", False):
                    self.logger.debug(
                        f"  Règle '{rule_name}' échouée: Signal 'entry_confirmation_bullish' non détecté."
                    )
                    return False
            elif signal == "entry_confirmation_bearish":
                if not asset_signals.get("entry_confirmation_bearish", False):
                    self.logger.debug(
                        f"  Règle '{rule_name}' échouée: Signal 'entry_confirmation_bearish' non détecté."
                    )
                    return False
            elif signal == "validated_ob":
                if not asset_signals.get("validated_ob", False):
                    self.logger.debug(
                        f"  Règle '{rule_name}' échouée: Signal 'validated_ob' non détecté."
                    )
                    return False
            elif not asset_signals.get(signal, False):
                self.logger.debug(
                    f"  Règle '{rule_name}' échouée: Signal requis '{signal}' non détecté."
                )
                return False

        signal_must_not_contain = conditions.get("signal_must_not_contain", [])
        for signal in signal_must_not_contain:
            if asset_signals.get(f"{signal}_detected", False) or (
                signal in asset_signals and asset_signals.get(signal) is not False
            ):
                self.logger.debug(
                    f"  Règle '{rule_name}' échouée: Signal interdit '{signal}' détecté."
                )
                return False

        signal_details_must_validate = conditions.get(
            "signal_details_must_validate", {}
        )
        for signal_key, checks in signal_details_must_validate.items():
            signal_data = asset_signals.get(signal_key, {})
            if not signal_data or not isinstance(signal_data, dict):
                self.logger.debug(
                    f"  Règle '{rule_name}' échouée: Détails du signal '{signal_key}' manquants ou mal formés."
                )
                return False
            for detail_key, condition in checks.items():
                value_to_check = signal_data
                try:
                    for key_part in detail_key.split("."):
                        value_to_check = value_to_check.get(key_part)
                except AttributeError:
                    value_to_check = None
                if value_to_check is None:
                    self.logger.debug(
                        f"  Règle '{rule_name}' échouée: Détail '{detail_key}' du signal '{signal_key}' manquant."
                    )
                    return False
                if isinstance(condition, (bool, str, int, float)):
                    if value_to_check != condition:
                        self.logger.debug(
                            f"  Règle '{rule_name}' échouée: Détail '{detail_key}' ({value_to_check}) ne correspond pas à la valeur requise ({condition})."
                        )
                        return False
                elif isinstance(condition, dict):
                    for op, val in condition.items():
                        if not self._compare_values(value_to_check, op, val):
                            self.logger.debug(
                                f"  Règle '{rule_name}' échouée: Comparaison du détail '{detail_key}' ({value_to_check}) avec l'opérateur '{op}' et la valeur '{val}' a échoué."
                            )
                            return False
        self.logger.debug(f"  Règle '{rule_name}' passée avec succès.")
        return True

    def _compare_values(self, actual_value, operator, expected_value) -> bool:
        """Fonction d'aide pour gérer les comparaisons numériques. Déplacée de ConfigManager."""
        if not isinstance(actual_value, (int, float)):
            return False
        if operator == ">":
            return actual_value > expected_value
        if operator == "<":
            return actual_value < expected_value
        if operator == ">=":
            return actual_value >= expected_value
        if operator == "<=":
            return actual_value <= expected_value
        if operator == "==":
            return actual_value == expected_value
        return False

    def _pip_size_from(si: dict, signals: dict) -> float:

        try:
            point = float(
                (si.get("point") if isinstance(si, dict) else getattr(si, "point", 0.0))
                or signals.get("point")
                or 0.0
            )
            digits = int(
                (si.get("digits") if isinstance(si, dict) else getattr(si, "digits", 5))
                or 5
            )
            pip_points = 10.0 if digits in (3, 5) else 1.0
            pip_size = point * pip_points
            return pip_size if math.isfinite(pip_size) and pip_size > 0 else 0.0
        except Exception:
            return 0.0

    def _atr_pips_from_df(df_m1, window: int, pip_size: float) -> float:

        if df_m1 is None or df_m1.empty or pip_size <= 0:
            return float("nan")
        # TR = max( high-low, abs(high-prev_close), abs(low-prev_close) )
        h = df_m1["high"].values
        l = df_m1["low"].values
        c = df_m1["close"].values
        prev_c = np.roll(c, 1)
        prev_c[0] = c[0]
        tr = np.maximum(h - l, np.maximum(np.abs(h - prev_c), np.abs(l - prev_c)))
        w = max(int(window), 1)
        atr_points = np.mean(tr[-w:])  # EMA pas indispensable ici pour le contrôle soft
        atr_pips = atr_points / pip_size
        return float(atr_pips)

    def calculate_risk_parameters(
        self, context: dict, current_config: dict, trade_decision: dict
    ) -> dict:
        """
        Évalue les niveaux de risque/targets (SL/TP) et la cohérence du trade.
        ❗️Le SIZING (volume) est désormais délégué à TradeExecutor._calculate_risk_based_volume.
        Ici : on NE calcule plus le volume. On renvoie 'volume': None.

        Priorités des niveaux (de la plus forte à la plus faible):
        1) target_sl_pips / target_tp_pips        — décision de base
        2) sl_price / tp_price                    — si fournis explicitement en prix
        3) fallback via default_sl_pips (si défini en config risk_management)

        Refus explicites (hard):
        - action/symbole/prix invalide
        - incohérence directionnelle (BUY: sl<entry<tp ; SELL: tp<entry<sl)
        - equity nulle
        - contraintes broker (stops_level) impossibles à satisfaire
        """

        notes = []

        # --- 1) Entrées de base ---
        action = str(trade_decision.get("action", "")).upper()
        asset = str(trade_decision.get("asset", "")).upper()
        if action not in {"BUY", "SELL"} or not asset:
            return {"ok": False, "reason": "invalid_action_or_asset"}

        md = (context.get("market_data") or {}).get(asset, {}) or {}
        symbol_info = md.get("symbol_info", {}) or {}
        account_info = context.get("account_info", {}) or {}

        # DataFrame ATR: priorité M1
        df_m1 = md.get("annotated_rates_df_m1")
        df = df_m1 if df_m1 is not None else md.get("annotated_rates_df")

        # Prix d’entrée
        entry = trade_decision.get("entry_price", md.get("current_price"))
        try:
            if entry is None:
                return {"ok": False, "reason": "missing_entry_price"}
            entry = float(entry)
            if not (entry == entry and entry > 0):  # nan/inf check
                return {"ok": False, "reason": "invalid_entry_price"}
        except Exception:
            return {"ok": False, "reason": "invalid_entry_price"}

        # --- 2) Broker/symbole ---
        contract = float(symbol_info.get("trade_contract_size", 100000.0) or 100000.0)
        point = float(symbol_info.get("point", 0.00001) or 0.00001)
        digits = int(symbol_info.get("digits", 5) or 5)

        stops_lvl_points = float(
            symbol_info.get("trade_stops_level", symbol_info.get("stops_level", 0.0))
            or 0.0
        )
        spread_pts = float(md.get("current_spread_points", 0.0) or 0.0)

        pip_points = 10.0 if digits in (3, 5) else 1.0
        pip_size = max(1e-12, point * pip_points)
        spread_pips = spread_pts / pip_points
        stops_level_pips = stops_lvl_points / pip_points
        min_stop_price_dist = stops_lvl_points * point

        # --- Contrôle dynamique du spread (guardrails) ---
        gr_spread = (current_config.get("guardrails") or {}).get("spread") or {}
        use_spread_guard = bool(gr_spread.get("enabled", False))

        max_spread_pips = float(gr_spread.get("max_spread_pips", 999.0))
        max_spread_points = float(gr_spread.get("max_spread_points", 9999))

        if use_spread_guard:
            if spread_pips > max_spread_pips:
                return {
                    "ok": False,
                    "reason": f"spread_too_high:{spread_pips:.2f}p > {max_spread_pips}p",
                }
            if spread_pts > max_spread_points:
                return {
                    "ok": False,
                    "reason": f"spread_points_too_high:{spread_pts:.1f} > {max_spread_points}",
                }
        else:
            notes.append("spread_check_disabled_by_guardrails")

        # --- 3) Risque (config) ---
        rm_cfg = (current_config or {}).get("risk_management", {}) or {}

        risk_pct = float(rm_cfg.get("risk_per_trade_pct", 0.0))  # sizing délégué
        min_rr = float(rm_cfg.get("min_rr", 0.0))  # neutre si absent
        max_tp_sl_ratio = float(
            rm_cfg.get("max_tp_to_sl_ratio", 999.0)
        )  # neutre si absent

        default_sl_pips = rm_cfg.get("default_sl_pips", None)
        default_sl_pips = (
            float(default_sl_pips) if default_sl_pips is not None else None
        )

        # Equity check
        equity = float(
            account_info.get("equity", account_info.get("balance", 0.0)) or 0.0
        )
        if equity <= 0:
            return {"ok": False, "reason": "no_equity"}

        # --- 4) SL/TP en pips ou en prix ---
        sl_pips_val = None
        tp_pips_val = None

        sl_pips_target = trade_decision.get("target_sl_pips")
        tp_pips_target = trade_decision.get("target_tp_pips")
        sl_price_in = trade_decision.get("sl_price")
        tp_price_in = trade_decision.get("tp_price")

        if isinstance(sl_pips_target, (int, float)) and isinstance(
            tp_pips_target, (int, float)
        ):
            sl_pips_val = float(sl_pips_target)
            tp_pips_val = float(tp_pips_target)
            notes.append("levels_from_target_pips")

        elif sl_price_in is not None:
            try:
                sl_price_in = float(sl_price_in)
                tp_price_in = float(tp_price_in) if tp_price_in is not None else None
            except Exception:
                return {"ok": False, "reason": "invalid_level_types"}
            sl_pips_val = abs(entry - sl_price_in) / pip_size
            tp_pips_val = abs(tp_price_in - entry) / pip_size if tp_price_in else None
            notes.append("levels_from_price")

        elif default_sl_pips is not None and default_sl_pips > 0:
            sl_pips_val = default_sl_pips
            tp_pips_val = None
            notes.append(f"used_default_sl:{default_sl_pips}p")

        else:
            return {"ok": False, "reason": "missing_sl_and_no_default_in_config"}

        if not (sl_pips_val and sl_pips_val > 0):
            return {"ok": False, "reason": "invalid_sl_distance"}

        # Reconstruire les prix
        sl_dist_price = sl_pips_val * pip_size
        tp_dist_price = tp_pips_val * pip_size if tp_pips_val else None

        if action == "BUY":
            sl_price = entry - sl_dist_price
            tp_price = entry + tp_dist_price if tp_dist_price else None
            if tp_price and not (sl_price < entry < tp_price):
                return {"ok": False, "reason": "levels_incoherent_for_buy"}
        else:  # SELL
            sl_price = entry + sl_dist_price
            tp_price = entry - tp_dist_price if tp_dist_price else None
            if tp_price and not (tp_price < entry < sl_price):
                return {"ok": False, "reason": "levels_incoherent_for_sell"}

        # --- 5) Stops level broker ---
        if min_stop_price_dist > 0:
            if sl_dist_price < min_stop_price_dist:
                sl_dist_price = min_stop_price_dist
                sl_pips_val = sl_dist_price / pip_size
                sl_price = (
                    entry - sl_dist_price if action == "BUY" else entry + sl_dist_price
                )
                notes.append(f"sl_raised_to_broker_min:{sl_pips_val:.2f}p")
            if tp_dist_price and tp_dist_price < min_stop_price_dist:
                tp_dist_price = min_stop_price_dist
                tp_pips_val = tp_dist_price / pip_size
                tp_price = (
                    entry + tp_dist_price if action == "BUY" else entry - tp_dist_price
                )
                notes.append(f"tp_raised_to_broker_min:{tp_pips_val:.2f}p")

        # --- 6) Rounding ---
        if isinstance(digits, int) and digits >= 0:
            sl_price = round(sl_price, digits)
            if tp_price:
                tp_price = round(tp_price, digits)

        # --- 7) RR ---
        rr = (
            (tp_dist_price / sl_dist_price)
            if (tp_dist_price and sl_dist_price > 0)
            else None
        )
        spread_comp_price = spread_pts * point
        effective_tp_dist = max(0.0, (tp_dist_price or 0.0) - spread_comp_price)
        rr_effective = (effective_tp_dist / sl_dist_price) if sl_dist_price > 0 else 0.0

        # --- 7bis) Facteur dynamique ATR (optionnel, piloté par config) ---
        atr_m1_pips = None
        try:
            if df is not None and not df.empty and "atr" in df.columns:
                atr_m1_pips = float(df["atr"].iloc[-1]) / pip_size
        except Exception:
            atr_m1_pips = None

        # Lecture config dynamique
        gr_vol = (current_config.get("guardrails") or {}).get("volatility") or {}
        use_volatility_guard = bool(gr_vol.get("enabled", False))

        target_min = float(
            rm_cfg.get("atr_target_min", gr_vol.get("min_atr_m1_pips", 0.0))
        )
        target_max = float(
            rm_cfg.get("atr_target_max", gr_vol.get("max_atr_m1_pips", 999.0))
        )
        low_factor = float(rm_cfg.get("low_atr_factor", 1.0))
        min_factor = float(rm_cfg.get("min_factor", 1.0))

        if atr_m1_pips and atr_m1_pips > 0:
            if use_volatility_guard:
                if atr_m1_pips < target_min:
                    vol_factor = low_factor
                    notes.append(
                        f"atr_low:{atr_m1_pips:.2f}p (<{target_min}) → facteur {vol_factor}"
                    )
                elif atr_m1_pips > target_max:
                    safe_ratio = (target_max / atr_m1_pips) if atr_m1_pips > 0 else 1.0
                    vol_factor = max(min_factor, safe_ratio)
                    notes.append(
                        f"atr_high:{atr_m1_pips:.2f}p (>{target_max}) → facteur {vol_factor:.2f}"
                    )
                else:
                    vol_factor = 1.0
                    notes.append(
                        f"atr_ok:{atr_m1_pips:.2f}p (zone [{target_min}-{target_max}]) → neutre"
                    )
                trade_decision["volatility_factor"] = vol_factor
            else:
                notes.append("atr_check_disabled_by_guardrails")

        # --- 8) Volume délégué ---
        notes.append("volume_delegated_to_executor")

        # --- 9) Sortie ---
        return {
            "ok": True,
            "volume": None,
            "rr": rr,
            "rr_effective": rr_effective,
            "entry_price": entry,
            "sl_price": sl_price,
            "tp_price": tp_price,
            "sl_pips": sl_dist_price / pip_size,
            "tp_pips": (tp_dist_price / pip_size) if tp_dist_price else None,
            "spread_pips": spread_pips,
            "stops_level_pips": stops_level_pips,
            "notes": notes,
            "risk_pct_info": risk_pct,
            "contract_info": contract,
        }
