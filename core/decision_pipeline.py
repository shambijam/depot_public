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
# === [IA SUPPRIMÉE - Session 23 Nov 2025] ===
# Import AIInterface retiré (module supprimé)
from core.utils import ConfigValidationError, TradeStatus
from strategy.scalping import ScalpingStrategy
from phase_observer.market_analyzer import MarketAnalyzer
# === [ORDERFLOW V6 SUPPRIMÉ - Session 28 Nov 2025] ===
# detect_orderflow_v6 supprimé → analyse intégrée dans ScalpingStrategy
# from phase_observer.detect_orderflow_v6.orderflow_v6 import detect_orderflow_v6
# from phase_observer.detect_orderflow_v6.logging_manager import Span



# PATCH PIPE-IMP-1 — import du pipeline (chemin: strategy/pipeline.py)
try:
    from strategy.pipeline import ScalpingPipeline
except Exception:
    ScalpingPipeline = None


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
        # === [IA SUPPRIMÉE - Session 23 Nov 2025] ===
        # Paramètre ai_interface_instance retiré (module supprimé)
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
        # === [IA SUPPRIMÉE - Session 23 Nov 2025] ===
        # self.ai_interface = ai_interface_instance (retiré)
        self.strategy_manager = strategy_manager_instance

        # ✅ Flags internes (hardcodés)
        self.enable_context = True
        self.enable_structure = True
        self.enable_multi_tf = True

        # ✅ Cache local des configs assets
        self.asset_configs: Dict[str, Dict[str, Any]] = {}
        for asset in ["EURUSD", "GBPUSD", "USDJPY"]:
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

        # PhaseObserver optionnel
        self.phase_observer = phase_observer_instance

        # Flag debug
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

        if self.phase_observer is not None and hasattr(
            self.phase_observer, "debug_confidence_logging"
        ):
            self.phase_observer.debug_confidence_logging = debug_flag

        self.debug_confidence_logging = debug_flag

        self.logger.info(
            f"DecisionPipeline initialisé (phase_observer={'present' if self.phase_observer else 'absent'}) "
            f"| debug_confidence_logging={self.debug_confidence_logging}"
        )
        # --- PATCH A: Features + hook Arbiter (scalping sans PhaseObserver) ---
        try:
            self.features = self.config_manager.get("features") or {}
        except Exception:
            self.features = {}

        # True = on laisse le PhaseObserver côté scalping ; False = on le bypasse
        self.scalping_phase_observer_enabled = bool(
            self.features.get("scalping_phase_observer", False)
        )

        # Hook optionnel : si un Arbiter est attaché ailleurs (ex: bootstrap), récupère-le
        # (sinon, laisse à None: le code en tiendra compte plus bas)
        self.arbiter = getattr(self, "arbiter", None)

        self.logger.info(
            f"[INIT] scalping_phase_observer_enabled={self.scalping_phase_observer_enabled} | arbiter={'present' if self.arbiter else 'absent'}"
        )

        # ✅ [MARKET ANALYZER - 03 DEC 2025] Pour fusion avec VWAP
        try:
            self.market_analyzer = MarketAnalyzer(
                config_manager=self.config_manager,
                logger=self.logger
            )
            self.logger.info("[INIT] MarketAnalyzer initialisé avec succès (fusion VWAP)")
        except Exception as e:
            self.logger.error(f"[INIT] Échec init MarketAnalyzer: {e}")
            self.market_analyzer = None

        # --- SCALPING PIPELINE (phase-free) ---
        try:
            self.scalping_pipeline = ScalpingPipeline(
                config_manager=self.config_manager,
                arbiter=getattr(self, "arbiter", None),
                logger=self.logger,
            )
        except Exception as _e:
            self.logger.warning(f"[INIT] ScalpingPipeline indisponible: {_e}")
            self.scalping_pipeline = None

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

    def institutional_decision_pipeline(
        self, context: Dict[str, Any]
    ) -> Dict[str, Any]:
        """
        Orchestration décisionnelle (Banque Privée)
        - ScalpingStrategy -> USDJPY, EURUSD, GBPUSD
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
            print(
                f"🕒 [SESSION] market_open={analyzed_context.get('is_market_open')} "
                f"(in_hours={analyzed_context.get('is_trading_hours')}, "
                f"is_trading_day={analyzed_context.get('is_trading_day')})"
            )
            
            print("🤖 [DECISION] Contexte analysé avec succès")
            
            # Gate: hors horaires → aucune nouvelle entrée
            if not analyzed_context.get("is_market_open", True):
                self.logger.info("[SESSION] Fenêtre fermée (hors horaires). Aucune nouvelle entrée.")
                print("⛔ [SESSION] Hors horaires: pas de nouvelles entrées.")
                base_cfg = self.config_manager.get_current_dynamic_config() or {}
                return {
                    "timestamp_utc": datetime.now(UTC).isoformat(),
                    "context": analyzed_context,
                    "config_used": base_cfg,
                    "scalping_decisions": [],
                    "final_decisions": [],
                    "final_decision": {},
                    "execution_context": {"sessions": {"blocked": True, "reason": "outside_trading_hours"}},
                    "decision_trace": [],
                }

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
            # === Injection des pré-signaux live (footprint intra-minute) ===
            live_pre_signals = context.get("live_pre_signals", {}) or {}
            for asset, pre_sig in live_pre_signals.items():
                if not isinstance(pre_sig, dict):
                    continue
                delta = pre_sig.get("footprint_delta")
                poc = pre_sig.get("footprint_poc")

                # Boost léger de confiance si signal footprint fort
                if (
                    isinstance(delta, (int, float)) and abs(delta) > 100
                ):  # seuil ajustable
                    sig = signals.get(asset, {})
                    old_conf = float(sig.get("confidence_score", 0.5))
                    sig["confidence_score"] = min(1.0, old_conf + 0.1)
                    sig["footprint_live_delta"] = delta
                    sig["footprint_live_poc"] = poc
                    print(
                        f"📊 [LIVE] Pré-signal {asset}: Δ={delta}, POC={poc} "
                        f"(confiance boostée {old_conf:.2f}→{sig['confidence_score']:.2f})"
                    )
                # --- Ajout PATCH: autoriser entrée anticipée si footprint extrême ---
                early_entry_threshold = 300  # ajustable (Δ en volume)
                if abs(delta) >= early_entry_threshold:
                    sig["early_entry_allowed"] = True
                    print(
                        f"⚡ [LIVE] Early entry signal activé sur {asset} (Δ={delta})"
                    )
                else:
                    sig["early_entry_allowed"] = False

            # Conteneurs séparés (séparation stricte des domaines)
            scalping_decisions: list = []

            # --- Helpers locaux ---
            def _norm_action(x: str) -> str:
                return (x or "").strip().upper()

            def _to_buy_sell(action: str) -> str:
                """
                Normalise toutes les variantes vers BUY / SELL uniquement.
                Gère LONG/SHORT, *_LIMIT, *_STOP, et laisse BUY/SELL inchangés.
                Lève ValueError si vide/inconnu.
                """
                a = (_norm_action(action) or "").upper()
                mapping = {
                    "LONG": "BUY",
                    "SHORT": "SELL",
                    "BUY_LIMIT": "BUY",
                    "SELL_LIMIT": "SELL",
                    "BUY_STOP": "BUY",
                    "SELL_STOP": "SELL",
                }
                if a in ("BUY", "SELL"):
                    return a
                if a in mapping:
                    return mapping[a]
                raise ValueError(f"Action invalide pour SL/TP: {action!r}")

            def _is_valid(dec: dict) -> bool:
                return _norm_action(dec.get("action")) in {"BUY", "SELL", "CLOSE"}

            def _ensure_asset(dec: dict, fallback_asset: str) -> None:
                if not dec.get("asset") or str(dec.get("asset")).upper() == "UNKNOWN":
                    dec["asset"] = fallback_asset

            # --- SCALPING (USDJPY only) ---
            if "USDJPY" in signals:
                # --- PATCH B: PhaseObserver conditionnel pour scalping ---
                inject_phase = (
                    self.phase_observer
                    if self.scalping_phase_observer_enabled
                    else None
                )
                scalping = self.strategy_manager.get_strategy_instance(
                    "scalping",
                    per_asset="USDJPY",
                    inject={
                        "mt5_connector": getattr(self, "mt5_connector", None),
                        "phase_observer": inject_phase,  # <-- conditionnel
                      
                        "risk_manager": getattr(self, "risk_manager", None),
                        "audit_logger": logging.getLogger("AuditLogger"),
                    },
                    strict=False,
                )

                if scalping:
                    try:
                        dec = scalping.evaluate_entry(
                            "USDJPY", analyzed_context, signals["USDJPY"]
                        )
                        if isinstance(dec, dict):

                            # ====== CAS SPÉCIAL BURST ======
                            if (
                                dec.get("rule_name") == "burst_scalping"
                                and isinstance(dec.get("burst_decisions"), list)
                                and dec["burst_decisions"]
                            ):
                                sublist = dec["burst_decisions"]

                                # Récupérer le basket_id s'il est présent dans les sous-ordres
                                basket_id = None
                                for sd in sublist:
                                    if sd.get("basket_id"):
                                        basket_id = sd["basket_id"]
                                        break

                                # Antidoublon: si un panier avec ce basket_id est déjà ouvert (d’après le contexte), on ignore
                                def _ctx_has_basket(ctx, asset, bid):
                                    try:
                                        for pos in ctx.get("open_positions") or []:
                                            sym = pos.get("symbol") or pos.get("asset")
                                            if str(sym) != str(asset):
                                                continue
                                            c = str(pos.get("comment") or "")
                                            m = pos.get("meta") or {}
                                            if bid and (
                                                bid == pos.get("basket_id")
                                                or bid == m.get("basket_id")
                                                or bid in c
                                            ):
                                                return True
                                            # Support du tag générique "burst:<id>" dans comment
                                            if c.startswith("burst:") and (
                                                not bid or bid in c
                                            ):
                                                return True
                                    except Exception:
                                        pass
                                    return False

                                if basket_id and _ctx_has_basket(
                                    analyzed_context, "USDJPY", basket_id
                                ):
                                    print(
                                        f"⛔ [SCALPING] burst ignoré: panier déjà ouvert ({basket_id})"
                                    )
                                else:
                                    # On pousse UNE décision maître uniquement
                                    master = sublist[0].copy()
                                    master["strategy_type"] = "scalping"
                                    _ensure_asset(master, "USDJPY")
                                    master.setdefault("execution_status", "ready")
                                    master.setdefault("rule_name", "burst_scalping")
                                    master["is_burst_trade"] = True
                                    master["burst_enabled"] = True
                                    master["burst_size"] = len(sublist)

                                    # Pack complet pour l'exécuteur (fan-out unique côté TradeExecutor)
                                    meta_master = master.setdefault("meta", {})
                                    meta_master["burst_package"] = sublist
                                    if basket_id:
                                        meta_master.setdefault("basket_id", basket_id)

                                    # Tag pour traçage/anti-duplication côté broker/positions
                                    if basket_id:
                                        master.setdefault(
                                            "comment", f"burst:{basket_id}"
                                        )
                                    else:
                                        master.setdefault("comment", "burst:auto")

                                    if _is_valid(master):
                                        scalping_decisions.append(master)
                                        print(
                                            f"✅ [SCALPING] burst (maître) retenu: {master.get('action')} "
                                            f"{master.get('asset')} size={len(sublist)} basket={basket_id}"
                                        )

                            # ====== CAS SCALPING NORMAL ======
                            else:
                                dec["strategy_type"] = "scalping"
                                _ensure_asset(dec, "USDJPY")
                                dec.setdefault("execution_status", "ready")
                                if _is_valid(dec):
                                    scalping_decisions.append(dec)
                                    print(
                                        f"✅ [SCALPING] décision retenue: {dec.get('action')} {dec.get('asset')} rule={dec.get('rule_name')}"
                                    )

                    except Exception as e:
                        self.logger.error(
                            f"[DECISION] Erreur scalping: {e}", exc_info=True
                        )

            # === Fusion pour compat héritage (tout en gardant les listes séparées) ===
            print(
                f"📦 scalping_decisions={len(scalping_decisions)}"
            )
            if scalping_decisions:
                print(
                    f"   ↳ top scalping: {scalping_decisions[0].get('action')} {scalping_decisions[0].get('asset')}"
                )

            final_decisions = scalping_decisions

            # === ÉTAPE 3: Choix principal (1 trade max / cycle) ===
            td = final_decisions[0] if final_decisions else {}
            chosen_strategy = td.get("strategy_type") if td else None
            chosen_asset = td.get("asset") if td else None

            # 🔥 PATCH: Intégrer les footprints dans la décision finale
            try:
                if td and chosen_asset and chosen_asset in signals:
                    # 1) Récupère l'historique footprints poussé par analyze_last_bar / MTF
                    fph = (
                        signals[chosen_asset].get("footprints_history", [])
                        or []
                    )
                    td["footprints_history"] = fph[
                        -5:
                    ]  # garde une fenêtre courte pour décision

                    # 2) Calcule un biais footprint simple sur les 3 derniers deltas
                    last3 = [
                        fp.get("delta", 0.0)
                        for fp in fph[-3:]
                        if isinstance(fp, dict)
                    ]
                    bias = "neutral"
                    if len(last3) == 3:
                        if all(d > 0 for d in last3):
                            bias = "long"
                        elif all(d < 0 for d in last3):
                            bias = "short"
                    td["footprint_bias"] = bias

                    # 3) Micro-boost de confiance si cohérence action ↔ biais footprint
                    try:
                        act = (td.get("action") or "").upper()
                        old_conf = float(
                            td.get(
                                "confidence_score",
                                signals[chosen_asset].get(
                                    "confidence_score", 0.5
                                ),
                            )
                        )
                        new_conf = old_conf
                        if (bias == "long" and act == "BUY") or (
                            bias == "short" and act == "SELL"
                        ):
                            new_conf = min(1.0, old_conf + 0.05)  # +5 bps
                        td["confidence_score"] = new_conf
                    except Exception:
                        pass

                    # 4) Propage aussi le flag early_entry si le signal l'autorise déjà
                    if signals[chosen_asset].get("early_entry_allowed", False):
                        td["early_entry_allowed"] = True

                    # 5) Log clair pour traçabilité
                    self.logger.info(
                        f"[Decision] Footprints: asset={chosen_asset} bias={bias} "
                        f"last3={last3} conf→{td.get('confidence_score')}"
                    )
            except Exception as e:
                self.logger.warning(
                    f"[Decision] Intégration footprints impossible: {e}"
                )

            # === ÉTAPE 4: Adaptation config (base + config stratégie choisie) ===
            if chosen_strategy:
                strat_cfg = (
                    self.strategy_manager.get_strategy_config(chosen_strategy) or {}
                )
            else:
                strat_cfg = {}  # ne JAMAIS appeler get_strategy_config(None)

            config_for_this_cycle = self.config_manager._merge_dicts(
                base_cfg, strat_cfg
            )
            adapted_config = (
                self.adapt_config(config_for_this_cycle, analyzed_context) or {}
            )
            print("🤖 [DECISION] Configuration adaptée avec succès")

            # === ÉTAPE 4bis: Execution context (léger) ===
            execution_context = {
                "spreads_pips": {},
                "katana_snapshots": {},
                "katana_ready_assets": [],
            }
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
                # Compat héritage
                "final_decisions": final_decisions,
                "final_decision": td,
                "execution_context": execution_context,
                "decision_trace": [],
            }

        except Exception as e:
            print(f"💥 [DECISION] ERREUR dans le pipeline: {e}")
            self.logger.error(
                f"Erreur critique dans institutional_decision_pipeline: {e}",
                exc_info=True,
            )
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
        Renvoie None si impossible (données manquantes / open_time None).
        """
        try:
            df = mkt.get("annotated_rates_df_m1") or mkt.get("annotated_rates_df")
            if df is None or len(df) == 0 or open_time is None:
                return None

            # Timestamps
            if "time" in df.columns:
                ts = pd.to_datetime(df["time"], errors="coerce", utc=True)
            elif isinstance(df.index, pd.DatetimeIndex):
                ts = df.index.tz_localize("UTC") if df.index.tz is None else df.index
            else:
                return None

            start = pd.to_datetime(open_time, unit="s", utc=True)
            return int((ts >= start).sum())
        except Exception:
            return None

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

        # 🔧 Init sécurité pour pip_size (utilisé plus bas même hors burst)
        pip_size = 0.0001

        try:
            from strategy.scalping import ScalpingStrategy
        except Exception as e:
            self.logger.error(f"[CORE] Impossible d'importer ScalpingStrategy: {e}")
            print(f"⚠️ [CORE] Erreur import stratégie: {e}")
            return {}

        # --- Priorité 1 : Scalping via Pipeline (phase-free) ---
        if "USDJPY" in signals:
            try:
                if getattr(self, "scalping_pipeline", None) is None:
                    # Fallback ultra-sécurisé au cas où l'init a échoué
                    from strategy.pipeline import ScalpingPipeline as _SP

                    self.scalping_pipeline = _SP(
                        self.config_manager,
                        arbiter=getattr(self, "arbiter", None),
                        logger=self.logger,
                    )

                decision = self.scalping_pipeline.run(
                    asset="USDJPY", context=context, current_config=current_config
                )
                if isinstance(decision, dict) and decision:
                    trade_decision = decision
                    self.logger.info(
                        "[CORE] Signal scalping (pipeline) retenu sur USDJPY"
                    )
                    print("✅ [CORE] Décision scalping (pipeline) détectée sur USDJPY")
            except Exception as e:
                self.logger.error(f"[CORE] Erreur ScalpingPipeline.run: {e}")

        # --- Aucun signal ---
        if not trade_decision:
            self.logger.info("[CORE] Aucun signal exploitable (scalping)")
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
        # --- PATCH C (ARB-01): Gate Arbiter avant exécution ---
        try:
            chosen_strategy_name = str(
                current_config.get("strategy_name", "unknown")
            ).lower()
        except Exception:
            chosen_strategy_name = "unknown"

        if self.arbiter and normalized_action in {"BUY", "SELL"}:
            try:
                ok, reason = self.arbiter.can_open(
                    asset_raw, normalized_action, chosen_strategy_name
                )
            except Exception as _e:
                ok, reason = True, f"ARB_ERROR:{_e}"
            if not ok:
                self.logger.info(
                    f"[ARB.BLOCK] {asset_raw} {normalized_action} par '{chosen_strategy_name}' refusé: {reason}"
                )
                print(f"⛔ [ARB] Blocage: {asset_raw} {normalized_action} ({reason})")
                return {}

        # ==========================================================
        # 📊 Analyse patterns / bougies (Desk Pro Mode via MarketAnalyzer)
        # ==========================================================
        try:
            md_asset = (context.get("market_data", {}) or {}).get(asset_raw, {}) or {}
            df_patterns = md_asset.get("annotated_rates_df") or md_asset.get("rates_df")

            if isinstance(df_patterns, pd.DataFrame) and not df_patterns.empty:
                ma = MarketAnalyzer(
                    config_manager=self.config_manager, logger=self.logger
                )
                ma_results = ma.analyze(df_patterns.copy(), asset_raw)

                last_sig = ma_results.get("latest", {})

                context.setdefault("pattern_analysis", {})[asset_raw] = {
                    "all_patterns": ma_results.get("patterns", {}),
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
            self.logger.warning(f"Erreur MarketAnalyzer: {e}")

        # ==========================================================
        # 🔗 PATCH D — Fusion Manager (Orderflow + FootprintTriggers + Stratégie)
        # ==========================================================
        try:
            # -- 1) Sources de données brutes (M1 + ticks) depuis le context --
            md_asset = (context.get("market_data", {}) or {}).get(asset_raw, {}) or {}
            df_m1 = (
                md_asset.get("annotated_rates_df_m1")
                or md_asset.get("annotated_rates_df")
                or md_asset.get("rates_df")
            )
            ticks_df = (
                md_asset.get("ticks")
                or md_asset.get("ticks_buffer")
                or md_asset.get("recent_ticks")
            )
            md_asset = (context.get("market_data", {}) or {}).get(asset_raw, {}) or {}
            df_m1 = (
                md_asset.get("annotated_rates_df_m1")
                or md_asset.get("annotated_rates_df")
                or md_asset.get("rates_df")
            )
            ticks_df = (
                md_asset.get("ticks")
                or md_asset.get("ticks_buffer")
                or md_asset.get("recent_ticks")
            )
            # --- INIT DÉFENSIF : toujours définis, même si les blocs suivants ne s’exécutent pas
            orderflow: Dict[str, Any] = {}
            trigger_ok: bool = False
            trigger: Dict[str, Any] = {}

            # -- 2) ORDERFLOW M1 (V6 institutionnel) : score + biais (delta_total) + VP (VPOC/VA) --
            orderflow = {}
            # === [ORDERFLOW V6 SUPPRIMÉ - Session 28 Nov 2025] ===
            # Ancienne analyse detect_orderflow_v6 supprimée
            # → Analyse OrderFlow V6 maintenant intégrée dans ScalpingStrategy._analyze_orderflow_v6()
            orderflow = {}


            # -- 4) Appel fusion avec VWAP via MarketAnalyzer (03 DEC 2025) --
            if self.market_analyzer is not None and df_m1 is not None:
                # ✅ CORRECTION (08 DEC 2025): Extraire latest depuis df_m1 si md_asset.latest vide
                latest_row = md_asset.get("latest")
                if not latest_row and df_m1 is not None and not df_m1.empty:
                    try:
                        latest_row = df_m1.iloc[-1]  # Dernière ligne du DataFrame
                    except Exception:
                        latest_row = {}

                # Construire market_results compatible avec build_fused_decision()
                market_results = {
                    "annotated_rates_df": df_m1,
                    "annotated_df": df_m1,  # Fallback
                    "latest": latest_row or {},
                    "patterns": {
                        "orderflow": orderflow or {}
                    }
                }

                fused = self.market_analyzer.build_fused_decision(
                    asset=asset_raw,
                    strategy_config=current_config,
                    footprint_trigger=trigger or {},
                    market_results=market_results,
                    context=context
                )
            else:
                # Fallback: ancienne fusion sans VWAP
                fused = self._fuse_signals_for_scalping(
                    asset=asset_raw,
                    strategy_decision=trade_decision.copy(),
                    orderflow=orderflow or {},
                    trigger=trigger or {},
                    current_config=current_config,
                    context=context,
                )

            # -- 5) Si la fusion produit une décision, on remplace la décision courante --
            if fused and isinstance(fused, dict) and fused.get("action"):
                trade_decision = fused
                print(
                    f"🧩 [FUSION] action={fused.get('action')} dir={fused.get('direction')} "
                    f"conf={float(fused.get('confidence', 0)):.2f} | {fused.get('rationale')}"
                )

        except Exception as e:
            self.logger.warning(f"[FUSION] erreur fusion: {e}")

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

        # 5) Sizing au risque (⚠️ sans AUCUN ajustement de volume ici)
        risk_params = self.calculate_risk_parameters(
            context, current_config, trade_decision
        )
        self.logger.debug(f"Paramètres de risque calculés: {risk_params}")

        if isinstance(risk_params, dict) and risk_params.get("ok"):
            # ✅ Propagation des niveaux (hints) — la finalisation se fait dans order_builder/sltp
            if risk_params.get("sl_price") is not None:
                trade_decision["sl_price"] = float(risk_params["sl_price"])
            if risk_params.get("tp_price") is not None:
                trade_decision["tp_price"] = float(risk_params["tp_price"])

            if risk_params.get("sl_pips") is not None:
                trade_decision["target_sl_pips"] = float(risk_params["sl_pips"])
            if risk_params.get("tp_pips") is not None:
                trade_decision["target_tp_pips"] = float(risk_params["tp_pips"])

        # 💡 RR dynamique → hint pour le moteur SLTP (utilisé par order_builder)
        try:
            bs_cfg = (
                (current_config.get("entry_rules", {}) or {}).get("scalping", {}) or {}
            ).get("burst_scalping", {}) or {}
            sltp_cfg = bs_cfg.get("sltp", {}) or {}

            rr_base = float(sltp_cfg.get("rr_base", 1.5) or 1.5)
            rr_floor = float(sltp_cfg.get("rr_floor", 1.0) or 1.0)
            rr_cap = float(sltp_cfg.get("rr_cap", 3.0) or 3.0)

            vol_factor = float(trade_decision.get("volatility_factor", 1.0) or 1.0)
            asset_sym = trade_decision.get("asset")

            # confiance 0..1 robuste
            try:
                conf = float(
                    (signals.get(asset_sym, {}) or {}).get("confidence_score", 0.5)
                )
                if not (0.0 <= conf <= 1.0):
                    conf = 0.5
            except Exception:
                conf = 0.5

            trigger_boost = 0.9 + 0.2 * conf  # 0→0.9 ; 0.5→1.0 ; 1→1.1
            rr_hint = rr_base * vol_factor * trigger_boost
            rr_hint = max(rr_floor, min(rr_cap, rr_hint))

            trade_decision["tp_rr_ratio_hint"] = float(rr_hint)

            # ===== SLTP HINTS (paquet unique pour l'Order Builder) =====
            sl = trade_decision.get("sl_price")
            tp = trade_decision.get("tp_price")
            sl_pips = trade_decision.get("target_sl_pips")
            tp_pips = trade_decision.get("target_tp_pips")
            rr_hint_val = trade_decision.get("tp_rr_ratio_hint")
            multi_tp_enabled = bool(sltp_cfg.get("multi_tp_enabled", False))

            entry_price = trade_decision.get("entry_price")
            entry_price = float(entry_price) if entry_price is not None else None

            trade_decision["sltp_hints"] = {
                "mode": "dynamic",  # SLTP dynamique actif
                "asset": asset_raw,
                "side": normalized_action,  # BUY/SELL
                "entry_price": entry_price,
                # Hints de niveaux si déjà calculés (risk engine)
                "sl_price": float(sl) if sl is not None else None,
                "tp_price": float(tp) if tp is not None else None,
                "sl_pips": float(sl_pips) if sl_pips is not None else None,
                "tp_pips": float(tp_pips) if tp_pips is not None else None,
                # RR à utiliser si pas de tp_price direct
                "rr_hint": float(rr_hint_val) if rr_hint_val is not None else None,
                # Autorisation de décomposer en multi-TP côté exécution
                "multi_tp_enabled": multi_tp_enabled,
            }

        except Exception as _e:
            self.logger.debug(f"[CORE] SLTP hints non construits: {_e}")

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
        # === Tag spécial pour Burst Scalping (SL/TP actif) ===
        if str(trade_decision.get("rule_name", "")).lower() == "burst_scalping":
            trade_decision["is_burst_trade"] = True
            trade_decision["burst_enabled"] = True
            # Expose burst_size au pipeline d'exécution ; fallback conf si absent
            try:
                bs = int(
                    trade_decision.get("burst_size")
                    or (
                        (current_config.get("entry_rules", {}) or {})
                        .get("scalping", {})
                        .get("burst_scalping", {})
                        .get("burst_size", 1)
                    )
                )
            except Exception:
                bs = 1
            trade_decision["burst_size"] = max(1, bs)
        else:
            trade_decision["is_burst_trade"] = False

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

        # [EXEC-01] Exécution immédiate : envoi au TradeExecutor
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
                # --- PATCH A: Centralisation exécution ---
                exec_res = run_trade_execution_pipeline(
                    te, decision_package, is_dry_run=False
                )
                self.logger.info(f"[EXECUTOR] Envoi MT5 terminé: {exec_res}")

                # --- Enrichir la décision avec le résultat d’exécution ---
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
                    # --- PATCH D (ARB-02): Register trade auprès de l’Arbiter quand exécuté ---
                    try:
                        if self.arbiter and trade_decision.get("executed"):
                            self.arbiter.register_trade(
                                asset_raw, normalized_action, chosen_strategy_name
                            )
                            self.logger.info(
                                f"[ARB.REG] {asset_raw} {normalized_action} enregistré (owner={chosen_strategy_name})"
                            )
                    except Exception as _e:
                        self.logger.debug(f"[ARB.REG] Ignoré (err={_e})")

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

    def _fuse_signals_for_scalping(
        *,
        asset: str,
        strategy_decision: Dict[str, Any],
        orderflow: Optional[Dict[str, Any]] = None,
        trigger: Optional[Dict[str, Any]] = None,
        current_config: Dict[str, Any],
        context: Dict[str, Any],
    ) -> Dict[str, Any]:
        # normalisation défensive
        orderflow = orderflow or {}
        trigger = trigger or {}
        
        """
        Fusionne 3 sources:
          - stratégie (pipeline scalping) -> action/direction + confidence
          - orderflow M1 (detect_orderflow_v6) -> score + biais (signe de delta_total)
          - triggers footprint temps-réel -> trigger_type + direction + confidence + meta{poc, delta_total, ...}
        Applique: validation, cohérence, règles métier, pondération, veto.
        Sortie normalisée: {action, direction, confidence, rule_name, meta{fused_debug...}, rationale}
        """

        def _norm_dir(x):
            x = str(x or "").upper()
            return x if x in ("BUY", "SELL") else "NEUTRAL"

        # --- 1) Extraction directions & scores ---
        strat_dir = _norm_dir(strategy_decision.get("action"))
        strat_conf = float(strategy_decision.get("confidence", 0.6) or 0.6)

        # Orderflow: biais par signe de delta_total (fallback neutre)
        of_score = float(orderflow.get("score", 0) or 0)
        dtot = float(((orderflow.get("summary") or {}).get("delta_total", 0)) or 0)
        of_dir = "BUY" if dtot > 0 else ("SELL" if dtot < 0 else "NEUTRAL")

        # Trigger footprint (conso: action/direction + confidence) :contentReference[oaicite:4]{index=4}
        trig_dir = _norm_dir(trigger.get("direction") or trigger.get("action"))
        trig_conf = float(trigger.get("confidence", 0.0) or 0.0)
        trig_type = str(trigger.get("trigger", "")).lower()

        # --- 2) Validation minimale (sinon on renvoie la décision stratégie telle quelle) ---
        if strat_dir == "NEUTRAL" and trig_dir == "NEUTRAL":
            return strategy_decision  # rien de mieux à fusionner

        # --- 3) Cohérence (votes) ---
        votes = [d for d in (strat_dir, of_dir, trig_dir) if d in ("BUY", "SELL")]
        buy_votes = sum(1 for v in votes if v == "BUY")
        sell_votes = sum(1 for v in votes if v == "SELL")
        majority_dir = (
            "BUY"
            if buy_votes > sell_votes
            else ("SELL" if sell_votes > buy_votes else strat_dir)
        )
        aligned_3 = buy_votes == 3 or sell_votes == 3
        aligned_2 = (not aligned_3) and (max(buy_votes, sell_votes) == 2)
        coherence_bonus = 0.15 if aligned_3 else (0.07 if aligned_2 else 0.0)
        conflict_malus = 0.0 if (aligned_3 or aligned_2) else 0.12

        # --- 4) Règles métier (veto/bonus) ---
        # Never against strong orderflow
        if of_score >= 80 and strat_dir in ("BUY", "SELL") and strat_dir != of_dir:
            return {
                "action": "NONE",
                "direction": "NEUTRAL",
                "confidence": 0.0,
                "rule_name": "fusion_veto_strong_orderflow",
                "meta": {
                    "fused_debug": {
                        "reason": "never_against_strong_orderflow",
                        "of_score": of_score,
                    }
                },
                "rationale": f"VETO: orderflow fort ({int(of_score)}) oppose la stratégie ({strat_dir}).",
            }

        # Veto absorption stricte (si trigger annonce une absorption contraire au flux)
        if "absorption" in trig_type and of_dir != "NEUTRAL" and trig_dir != of_dir:
            return {
                "action": "NONE",
                "direction": "NEUTRAL",
                "confidence": 0.0,
                "rule_name": "fusion_veto_absorption_conflict",
                "meta": {
                    "fused_debug": {"reason": "absorption_conflict", "trig": trig_type}
                },
                "rationale": "VETO: absorption footprint contraire à l’orderflow.",
            }

        # --- 5) Pondération (profil par défaut) ---
        # 50% trigger, 30% orderflow alignment, 20% stratégie  (+/- cohérence)
        align_component = (
            1.0
            if (trig_dir != "NEUTRAL" and trig_dir == of_dir and of_dir != "NEUTRAL")
            else (0.55 if of_dir != "NEUTRAL" else 0.5)
        )
        fused = (
            0.50 * (trig_conf or 0.5)
            + 0.30 * align_component
            + 0.20 * max(0.0, min(1.0, strat_conf))
        )
        fused = max(0.0, min(1.0, fused + coherence_bonus - conflict_malus))

        # --- 6) Direction finale ---
        final_dir = (
            majority_dir
            if majority_dir in ("BUY", "SELL")
            else (trig_dir if trig_dir in ("BUY", "SELL") else strat_dir)
        )
        if fused < float(current_config.get("entry_threshold_min", 0.55)):
            return {
                "action": "NONE",
                "direction": "NEUTRAL",
                "confidence": fused,
                "rule_name": "fusion_low_confidence",
                "meta": {
                    "fused_debug": {
                        "coherence_bonus": coherence_bonus,
                        "conflict_malus": conflict_malus,
                        "trig_conf": trig_conf,
                        "of_score": of_score,
                        "align_component": align_component,
                    }
                },
                "rationale": f"NO ENTRY: confiance fusionnée trop faible ({fused:.2f}).",
            }

        # --- 7) Rationale & retour normalisé ---
        rationale = []
        if final_dir == "BUY":
            rationale.append("BUY car")
        else:
            rationale.append("SELL car")
        if of_dir != "NEUTRAL":
            rationale.append(f"orderflow {of_dir.lower()} (score {int(of_score)})")
        if trig_dir != "NEUTRAL":
            rationale.append(
                f"trigger {trig_type or 'footprint'} {trig_dir.lower()} (conf {trig_conf:.2f})"
            )
        if aligned_3:
            rationale.append("+ triple alignement")
        elif aligned_2:
            rationale.append("+ double alignement")
        if coherence_bonus:
            rationale.append(f"+ bonus cohérence {coherence_bonus:.2f}")
        if conflict_malus:
            rationale.append(f"- malus conflit {conflict_malus:.2f}")

        out = {
            "action": final_dir,
            "direction": final_dir,
            "confidence": fused,
            "rule_name": (strategy_decision.get("rule_name") or "scalping")
            + "+fusion_manager",
            "asset": strategy_decision.get("asset", asset),
            "meta": {
                **(strategy_decision.get("meta") or {}),
                "fusion_details": {
                    "orderflow_score": of_score,
                    "orderflow_dir": of_dir,
                    "trigger_dir": trig_dir,
                    "trigger_conf": trig_conf,
                    "coherence_bonus": coherence_bonus,
                    "conflict_malus": conflict_malus,
                    "trigger_type": trig_type,
                },
            },
            "rationale": " ".join(rationale),
        }
        return out

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

        risk_pct = float(rm_cfg.get("risk_per_trade_percent", 0.0))  # sizing délégué
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
