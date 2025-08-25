# core/decision_pipeline.py
import logging
import json
import pandas as pd
import numpy as np
from datetime import datetime, UTC
from typing import Dict, Any, List, Optional, Tuple, TYPE_CHECKING
from pathlib import Path
from core.ai_interface import AIInterface
from core.utils import ConfigValidationError, TradeStatus  # NOUVEL IMPORT DEPUIS UTILS
from typing import Any, Dict, List, Optional, Tuple


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
        phase_observer_instance=None,  # <-- optionnel
    ):
        """
        Initialise le DecisionPipeline sans présumer de la présence d'un PhaseObserver.
        - Ne touche PAS à self.phase_observer si None
        - Lit le flag de debug depuis la config, et l'applique uniquement si un PhaseObserver est fourni
        """
        import logging

        self.logger = logging.getLogger(__name__)
        self.config_manager = config_manager_instance
        self.ai_interface = ai_interface_instance
        self.strategy_manager = strategy_manager_instance

        # PhaseObserver optionnel (peut être attaché plus tard via attach_phase_observer)
        self.phase_observer = phase_observer_instance

        # Flag de debug (on ne force rien si pas de phase_observer)
        # On essaie plusieurs chemins possibles, valeur par défaut False
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

    def attach_phase_observer(self, phase_observer):
        """Attache/met à jour le PhaseObserver après coup, en appliquant le flag de debug s'il existe."""
        self.phase_observer = phase_observer
        if hasattr(self.phase_observer, "debug_confidence_logging"):
            self.phase_observer.debug_confidence_logging = bool(
                getattr(self, "debug_confidence_logging", False)
            )
        self.logger.info("PhaseObserver attaché au DecisionPipeline.")

    def get_max_spread_pips(cfg_scalping: dict, symbol: str) -> float:
        v = (cfg_scalping or {}).get("max_spread_pips", 3.0)
        # Autorise un dict par symbole, sinon valeur unique
        return v.get(symbol, v.get("default", v)) if isinstance(v, dict) else v
    
    def _demo_unleash_override(self, asset_symbol: str, context: dict | None = None) -> tuple[bool | None, str]:
        """
        Unleash DEMO: si activé dans la config, on bypass le gate Katana en mode DEMO.
        - Retourne (True, "demo_unleash") pour forcer l'entrée.
        - Retourne (None, "") pour ne rien faire (continuer le gate normal).
        Clés de config utilisées:
        - debug.unleash.allow_all_entries_demo: bool (False par défaut)
        - debug.unleash.max_spread_points: int | None (optionnel, None = aucune limite)
        - debug.unleash.max_positions: int | None (optionnel, None = pas de limite)
        """
        try:
            # 1) Flag principal
            allow_unleash = bool(self.config_manager.get("debug.unleash.allow_all_entries_demo", False))
            if not allow_unleash:
                return (None, "")
            # 2) Mode: uniquement en DEMO
            mode = str(self.config_manager.get("mode_execution", "DEMO")).upper()
            if mode != "DEMO":
                return (None, "")

            # 3) Garde-fous optionnels pour éviter des situations absurdes même en unleash
            #    a) Spread max
            try:
                max_spread_pts = self.config_manager.get("debug.unleash.max_spread_points", None)
            except Exception:
                max_spread_pts = None
            if max_spread_pts is not None:
                try:
                    last_spread = float(context.get("market_data", {}).get("spread_points", float("inf")))
                except Exception:
                    last_spread = float("inf")
                if not (last_spread <= float(max_spread_pts)):
                    return (None, "")

            #    b) Limite positions ouvertes
            try:
                max_pos = self.config_manager.get("debug.unleash.max_positions", None)
            except Exception:
                max_pos = None
            if max_pos is not None:
                try:
                    open_pos_count = int(context.get("account_state", {}).get("open_positions_count", 0))
                except Exception:
                    open_pos_count = 0
                if open_pos_count >= int(max_pos):
                    return (None, "")

            # OK: bypass
            return (True, "demo_unleash")
        except Exception:
            # En cas d’erreur, ne pas bloquer le flux normal — on laisse le gate standard décider.
            return (None, "")


    def institutional_decision_pipeline(
        self, context: Dict[str, Any]
    ) -> Dict[str, Any]:
        """
        Orchestre le pipeline de décision de haut niveau pour un cycle de trading.
        Version enrichie (Katana) : construit un execution_context (spreads, snapshots Katana, métriques)
        pour l'exécuteur & l'audit.
        """
        self.logger.info("--- Démarrage du Pipeline de Décision Institutionnel ---")
        print(f"🤖 [DECISION] Début du pipeline institutionnel")

        try:
            # 1) Analyse et enrichissement du contexte
            print(f"🤖 [DECISION] Étape 1: Analyse du contexte...")
            analyzed_context = self.config_manager.analyze_context(context)
            print(f"🤖 [DECISION] Contexte analysé avec succès")

            # 2) (IA désactivée ici – audit asynchrone ailleurs)
            print(f"🤖 [DECISION] Étape 2: Vérification IA...")
            print(f"🤖 [DECISION] IA désactivée")

            # 3) Sélection de la stratégie optimale
            print(f"🤖 [DECISION] Étape 3: Sélection de stratégie...")
            if not self.strategy_manager:
                self.logger.critical(
                    "ERREUR ARCHITECTURALE: StrategyManager non disponible dans DecisionPipeline."
                )
                raise RuntimeError("StrategyManager non initialisé.")
            config_knowledge_base = self.strategy_manager.strategy_registry
            print(f"🤖 [DECISION] Base de connaissances: {len(config_knowledge_base)} stratégies disponibles")

            optimal_config = self.select_optimal_config(analyzed_context, config_knowledge_base)
            if not optimal_config:
                self.logger.warning("Aucune stratégie optimale sélectionnée pour ce cycle. Pipeline arrêté.")
                print(f"🤖 [DECISION] ❌ Aucune stratégie optimale trouvée")
                return {
                    "timestamp_utc": datetime.now(UTC).isoformat(),
                    "context": analyzed_context,
                    "config_used": self.config_manager.get_current_dynamic_config(),
                    "final_decision": {},
                    "execution_context": {},
                }

            print(f"🤖 [DECISION] ✅ Stratégie optimale: {optimal_config.get('strategy_name', 'Unknown')}")

            # 4) Adaptation de la configuration pour le cycle actuel
            print(f"🤖 [DECISION] Étape 4: Adaptation de configuration...")
            base_cfg = self.config_manager.get_current_dynamic_config()
            config_for_this_cycle = self.config_manager._merge_dicts(base_cfg, optimal_config)
            adapted_config = self.adapt_config(config_for_this_cycle, analyzed_context)
            print(f"🤖 [DECISION] Configuration adaptée avec succès")

            # 4bis) Execution context (spreads, katana snapshots, métriques)
            print(f"🤖 [DECISION] Étape 4bis: Construction execution_context...")
            tradeables = adapted_config.get("tradeable_assets", []) or []
            spreads_pips: Dict[str, float] = {}
            katana_snapshots: Dict[str, Dict[str, Any]] = {}
            katana_ready_assets: List[str] = []

            # spreads par actif (robuste)
            if hasattr(self, "mt5_connector"):
                for a in tradeables:
                    try:
                        sp = self.mt5_connector.get_spread_pips(self.config_manager.get("asset_symbol_mapping", {}).get(a, a))
                    except Exception:
                        sp = float("inf")
                    spreads_pips[a] = float(sp) if isinstance(sp, (int, float)) else float("inf")

            # snapshots Katana (si PhaseObserver expose la méthode)
            if hasattr(self, "phase_observer") and hasattr(self.phase_observer, "get_katana_snapshot"):
                for a in tradeables:
                    try:
                        snap = self.phase_observer.get_katana_snapshot(a, adapted_config) or {}
                    except Exception:
                        snap = {"katana_ready": False, "reason": "snapshot_error"}
                    katana_snapshots[a] = snap
                    if snap.get("katana_ready"):
                        katana_ready_assets.append(a)

            execution_context = {
                "spreads_pips": spreads_pips,
                "katana_snapshots": katana_snapshots,
                "katana_ready_assets": katana_ready_assets,
            }
            analyzed_context["execution_context"] = execution_context  # pour consommation ultérieure (mecano/audit)

            # 5) Décision de trade finale
            print(f"🤖 [DECISION] Étape 5: Décision de trade finale...")
            signals = analyzed_context.get("trading_signals", {}) or {}
            print(f"🤖 [DECISION] Signaux disponibles: {list(signals.keys())}")
            trade_decision = self.decide_trade_to_execute(
                analyzed_context,
                adapted_config,
                signals,
            )
            print(f"🤖 [DECISION] Décision finale: {trade_decision.get('action', 'AUCUNE')}")

            # 5bis) RR projeté simple si overrides pips présents (utile pour audit)
            tp_pips = trade_decision.get("target_tp_pips")
            sl_pips = trade_decision.get("target_sl_pips")
            rr_projected = None
            try:
                if isinstance(tp_pips, (int, float)) and isinstance(sl_pips, (int, float)) and sl_pips > 0:
                    rr_projected = float(tp_pips) / float(sl_pips)
            except Exception:
                rr_projected = None

            # Marquer les métas utiles à l'exécuteur/audit
            trade_decision["meta_rr_projected"] = rr_projected
            # Si l’actif choisi a un snapshot, passer quelques métas utiles (ex: atr_m1_pips)
            chosen_asset = trade_decision.get("asset")
            if chosen_asset and chosen_asset in katana_snapshots:
                snap = katana_snapshots[chosen_asset] or {}
                trade_decision["meta_atr_m1_pips"] = snap.get("atr_m1_pips")
                trade_decision["meta_katana_score"] = snap.get("katana_score")

            return {
                "timestamp_utc": datetime.now(UTC).isoformat(),
                "context": analyzed_context,
                "config_used": adapted_config,
                "final_decision": trade_decision,
                "execution_context": execution_context,
            }

        except Exception as e:
            print(f"💥 [DECISION] ERREUR dans le pipeline: {e}")
            self.logger.error(f"Erreur critique dans institutional_decision_pipeline: {e}", exc_info=True)
            return {
                "timestamp_utc": datetime.now(UTC).isoformat(),
                "context": context,
                "config_used": self.config_manager.get_current_dynamic_config(),
                "final_decision": {},
                "execution_context": {},
                "error": str(e),
            }

    def score_configs(
        self, context: Dict[str, Any], configs: Dict[str, Any]
    ) -> Dict[str, float]:
        """
        Évalue et note les configurations de stratégies disponibles en fonction du contexte.
        Version purgée : aucune logique spécifique à une stratégie hors whitelist.
        """
        self.logger.info("Évaluation des configurations de stratégies disponibles...")

        market_regime = context.get("current_market_regime", "unknown_regime_fallback")
        self.logger.debug(f"Régime de marché actuel pour le scoring: {market_regime}")

        # === 0) Filtrage par liste blanche de stratégies autorisées ===
        allowed_strategies = {"scalping", "dynamic", "liquidity"}
        eligible_configs: Dict[str, Any] = {}
        for path, data in configs.items():
            actual_config = data["config"] if "config" in data else data
            strategy_name = str(actual_config.get("strategy_name", "")).lower()
            if strategy_name not in allowed_strategies:
                self.logger.debug(
                    f"[SCORING] Stratégie ignorée (non autorisée): '{strategy_name}' depuis '{Path(path).name}'"
                )
                continue
            eligible_configs[path] = data

        if not eligible_configs:
            self.logger.warning(
                "[SCORING] Aucune stratégie éligible après filtrage whitelist."
            )
            return {}

        # Préparation des données pour scoring
        trading_signals = context.get("trading_signals", {})
        config_scores: Dict[str, float] = {}
        strategy_weights = self.config_manager.get("scoring_rules.strategy_weights", {})
        risk_thresholds = self.config_manager.get(
            "scoring_rules.risk_appetite_drawdown_thresholds", {}
        )

        print(f"🎯 [SCORING] Début évaluation {len(eligible_configs)} stratégies")
        print(f"🎯 [SCORING] Signaux disponibles: {list(trading_signals.keys())}")

        for path, data in eligible_configs.items():
            # === DEBUG STRUCTURE DES DONNÉES ===
            print(f"🔍 [DEBUG] Path: {path}")
            print(f"🔍 [DEBUG] Data keys: {list(data.keys())}")
            print(f"🔍 [DEBUG] Data type: {type(data)}")

            if "content" in data:
                print(f"🔍 [DEBUG] Content keys: {list(data['content'].keys())}")
                print(
                    f"🔍 [DEBUG] Content strategy_name: {data['content'].get('strategy_name', 'NOT_IN_CONTENT')}"
                )
            else:
                print(
                    f"🔍 [DEBUG] Direct strategy_name: {data.get('strategy_name', 'NOT_IN_DATA')}"
                )

            print(f"🔍 [DEBUG] Full data structure: {str(data)[:200]}...")
            print("=" * 50)

            actual_config = data["config"] if "config" in data else data
            strategy_name = str(actual_config.get("strategy_name", "")).lower()
            strategy_tags = actual_config.get("strategy_tags", [])

            # Debug line APRÈS définition des variables
            print(
                f"🔍 [DEBUG] Strategy name extracted: '{strategy_name}' from config: {actual_config.get('strategy_name', 'NOT_FOUND')}"
            )
            print(f"🔍 [SCORING] Évaluation stratégie: {strategy_name}")

            # === LOGIQUE SCALPING INTELLIGENTE ===
            if strategy_name == "scalping":
                score = self._calculate_enhanced_scalping_score(
                    actual_config, context, trading_signals, strategy_weights
                )
                print(f"🗡️ [SCALPING] Score final: {score:.3f}")

            # === LOGIQUE STANDARD POUR AUTRES STRATÉGIES ===
            else:
                score = self.config_manager.get("scoring_rules.base_score", 0.5)
                self.logger.debug(
                    f"Scoring stratégie '{strategy_name}' (Tags: {strategy_tags})"
                )

                # Compatibilité tags / régime
                for tag, weight in strategy_weights.items():
                    if tag in strategy_tags and tag in market_regime:
                        score += weight
                        self.logger.debug(
                            f"  + Score pour tag '{tag}' correspondant au régime. Score: {score}"
                        )
                    elif (
                        tag in strategy_tags
                        and strategy_name.startswith(tag)
                        and tag in market_regime.split("_")
                    ):
                        score += weight
                        self.logger.debug(
                            f"  + Score pour compatibilité ancienne de tag/régime. Score: {score}"
                        )

                # Appétit au risque vs drawdown
                risk_appetite = context.get("risk_appetite", "medium")
                max_dd = actual_config.get("max_drawdown_percent", 5.0)

                if risk_appetite == "low" and max_dd < risk_thresholds.get(
                    "low_risk_max_drawdown", 3.0
                ):
                    score += risk_thresholds.get("low_risk_score_boost", 0.1)
                    self.logger.debug(
                        f"  + Score boost pour appétit au risque 'bas' et faible DD. Score: {score}"
                    )
                elif risk_appetite == "high" and max_dd > risk_thresholds.get(
                    "high_risk_min_drawdown", 7.0
                ):
                    score += risk_thresholds.get("high_risk_score_boost", 0.05)
                    self.logger.debug(
                        f"  + Score boost pour appétit au risque 'élevé' et DD plus important. Score: {score}"
                    )

                # Reco IA
                current_config_path = path
                ai_recommendation_for_this_config_score = context.get(
                    "ai_recommendation_score", {}
                ).get(Path(current_config_path).name, 0.0)
                ai_weight = self.config_manager.get(
                    "scoring_rules.ai_recommendation_weight", 0.2
                )
                score += ai_recommendation_for_this_config_score * ai_weight
                self.logger.debug(
                    f"  + Score IA pour '{strategy_name}': {ai_recommendation_for_this_config_score * ai_weight}. Score: {score}"
                )

                # Performance historique
                historical_performance = data.get("performance", {})
                if historical_performance:
                    sharpe_ratio = historical_performance.get("sharpe_ratio", 0.0)
                    if sharpe_ratio > self.config_manager.get(
                        "scoring_rules.performance_thresholds.good_sharpe", 1.0
                    ):
                        score += self.config_manager.get(
                            "scoring_rules.performance_thresholds.good_sharpe_boost",
                            0.1,
                        )
                    elif sharpe_ratio < self.config_manager.get(
                        "scoring_rules.performance_thresholds.poor_sharpe", 0.5
                    ):
                        score -= self.config_manager.get(
                            "scoring_rules.performance_thresholds.poor_sharpe_penalty",
                            0.1,
                        )
                    self.logger.debug(
                        f"  + Score performance historique (Sharpe: {sharpe_ratio}). Score: {score}"
                    )

                print(f"📊 [STANDARD] {strategy_name} score: {score:.3f}")

            config_scores[path] = max(0.0, min(1.0, score))

        # Log final des scores
        print(f"\n🏆 [SCORING] RÉSULTATS FINAUX:")
        sorted_scores = sorted(config_scores.items(), key=lambda x: x[1], reverse=True)
        for path, score in sorted_scores:
            strategy_name_display = (
                eligible_configs[path].get("config", {}).get("strategy_name", "Unknown")
            )
            print(
                f"   {strategy_name_display:>10}: {score:.3f} {'🥇' if score == sorted_scores[0][1] else ''}"
            )

        self.logger.info(
            f"Évaluation des configurations terminée. Scores : {config_scores}"
        )
        return config_scores

    def select_assets_to_trade(self, context: Dict[str, Any]) -> List[str]:
        """
        Évalue, score et sélectionne dynamiquement les meilleurs actifs à trader pour le cycle actuel.
        Déplacée de ConfigManager.
        (Version purgée : exclusion définitive des actifs crypto)
        """
        self.logger.info("Sélection dynamique et scoring des actifs éligibles...")

        # Candidats initiaux depuis les signaux du contexte
        opportunities_candidates = list(context.get("trading_signals", {}).keys())

        if not opportunities_candidates:
            self.logger.info(
                "Aucune opportunité candidate à filtrer pour l'IA (liste vide)."
            )
            return []

        # --- 0) Filtre anti-crypto robuste ---
        def _is_crypto_symbol(sym: str) -> bool:
            if not isinstance(sym, str):
                return False
            s = sym.upper()
            # Denylist explicite + motifs communs
            if s in {"BTCUSD", "ETHUSD", "LTCUSD"}:
                return True
            return any(
                k in s
                for k in (
                    "BTC",
                    "ETH",
                    "LTC",
                    "DOGE",
                    "XRP",
                    "SOL",
                    "ADA",
                    "BNB",
                    "DOT",
                    "MATIC",
                )
            )

        before = list(opportunities_candidates)
        opportunities_candidates = [
            a for a in opportunities_candidates if not _is_crypto_symbol(a)
        ]
        removed = [a for a in before if a not in opportunities_candidates]
        if removed:
            self.logger.debug(
                f"[FILTER] Actifs crypto retirés de la sélection: {removed}"
            )

        if not opportunities_candidates:
            self.logger.info("Aucun actif non-crypto à considérer après filtrage.")
            return []

        final_opportunities_for_ai: List[str] = []

        # Groupes corrélés (majors FX)
        major_fx_pairs = self.config_manager.get(
            "ai.opportunity_filtering.major_fx_pairs_for_correlation",
            ["EURUSD", "GBPUSD", "USDJPY"],
        )
        processed_correlated_groups: set = set()

        # Seuil minimum de confiance du signal pour l'IA
        min_ai_signal_confidence = self.config_manager.get(
            "ai.opportunity_filtering.min_signal_confidence", 0.6
        )

        for asset in opportunities_candidates:
            print(f"🔍 [FILTER] Évaluation de {asset}...")

            if asset in processed_correlated_groups:
                print(f"❌ [FILTER] {asset} éliminé : corrélation")
                self.logger.debug(
                    f"Actif {asset} ignoré pour la shortlist AI car déjà couvert par un actif corrélé."
                )
                continue

            current_asset_signals = context.get("trading_signals", {}).get(asset, {})
            print(
                f"🔍 [FILTER] {asset} - Signaux: phase={current_asset_signals.get('phase')}, "
                f"confidence={current_asset_signals.get('confidence_score')}"
            )

            # Validation des signaux
            if (
                not current_asset_signals
                or not isinstance(current_asset_signals.get("phase"), str)
                or not isinstance(
                    current_asset_signals.get("confidence_score"), (int, float)
                )
            ):
                self.logger.debug(
                    f"Actif {asset} écarté : signaux de trading manquants, malformés ou incomplets."
                )
                continue

            current_asset_phase = current_asset_signals.get("phase", "")
            current_asset_confidence = float(
                current_asset_signals.get("confidence_score", 0.0)
            )

            # Filtre par confiance
            if current_asset_confidence < float(min_ai_signal_confidence):
                print(
                    f"❌ [FILTER] {asset} éliminé : confiance {current_asset_confidence:.2f} "
                    f"< seuil {float(min_ai_signal_confidence):.2f}"
                )
                self.logger.debug(
                    f"Actif {asset} écarté : confiance du signal ({current_asset_confidence:.2f}) "
                    f"inférieure au seuil min de l'IA ({float(min_ai_signal_confidence):.2f})."
                )
                continue

            # Charger la config spécifique de l'actif (sécurisé)
            try:
                asset_specific_config = (
                    self.config_manager.config_loader.load_asset_config(asset)
                )
            except Exception as e:
                self.logger.debug(
                    f"[FILTER] Échec chargement config asset '{asset}' ({e}). Fallback configuration vide."
                )
                asset_specific_config = {}

            is_relevant_for_ai = True

            # Vérifier la phase d'intérêt (si définie dans la config)
            phases_of_interest_for_asset = asset_specific_config.get(
                "phases_of_interest", {}
            )
            phase_type = current_asset_phase.split("_")[0]
            if (
                phases_of_interest_for_asset
                and phase_type not in phases_of_interest_for_asset
            ):
                self.logger.debug(
                    f"Actif {asset} écarté : phase '{current_asset_phase}' non listée comme d'intérêt dans la config de l'actif."
                )
                is_relevant_for_ai = False

            # Liquidité (selon PhaseObserver)
            if not current_asset_signals.get("is_liquid", False):
                print(f"❌ [FILTER] {asset} éliminé : non liquide")
                self.logger.debug(
                    f"Actif {asset} écarté : non liquide (PhaseObserver)."
                )
                is_relevant_for_ai = False

            # Spread max autorisé par actif
            max_allowed_spread_config = asset_specific_config.get("volatility", {}).get(
                "max_allowed_spread_points", {}
            )
            if isinstance(max_allowed_spread_config, dict):
                max_allowed_spread_points_for_asset = float(
                    max_allowed_spread_config.get("value", 4000)
                )
            else:
                max_allowed_spread_points_for_asset = float(
                    max_allowed_spread_config or 4000
                )

            current_spread_points = float(
                current_asset_signals.get("current_spread_points", np.inf)
            )
            if current_spread_points > max_allowed_spread_points_for_asset:
                self.logger.debug(
                    f"Actif {asset} écarté : spread ({current_spread_points}) dépasse "
                    f"le max autorisé par actif ({max_allowed_spread_points_for_asset})."
                )
                is_relevant_for_ai = False

            if is_relevant_for_ai:
                final_opportunities_for_ai.append(asset)
                print(f"✅ [FILTER] {asset} accepté pour l'IA")

                # Gestion de corrélation simple entre EURUSD/GBPUSD
                if asset in major_fx_pairs:
                    if asset == "EURUSD":
                        processed_correlated_groups.add("GBPUSD")
                    elif asset == "GBPUSD":
                        processed_correlated_groups.add("EURUSD")

        self.logger.info(
            f"Shortlist d'opportunités pour l'IA après filtrage : {final_opportunities_for_ai}"
        )
        return final_opportunities_for_ai

    def _calculate_enhanced_scalping_score(
        self, config: Dict, context: Dict, trading_signals: Dict, strategy_weights: Dict
    ) -> float:
        """
        🗡️ SCORING INTELLIGENT SCALPING - Reconnaissance des conditions KATANA

        Utilise votre configuration sophistiquée et vos signaux PhaseObserver
        pour détecter les opportunités scalping optimales
        """
        # Score de base
        base_score = self.config_manager.get("scoring_rules.base_score", 0.5)
        scalping_weight = strategy_weights.get("scalping", 0.2)
        score = base_score + scalping_weight

        print(
            f"🗡️ [SCALPING] Score base: {score:.3f} ({base_score} + {scalping_weight})"
        )

        # Analyser TOUS les assets pour trouver les meilleures conditions
        best_asset_score = 0.0
        best_asset = None
        scalping_opportunities = 0

        for asset, signals in trading_signals.items():
            asset_score = self._evaluate_scalping_asset_conditions(
                asset, signals, config
            )
            if asset_score > best_asset_score:
                best_asset_score = asset_score
                best_asset = asset
            if asset_score > 0.6:  # Seuil d'opportunité
                scalping_opportunities += 1

        # Bonus basé sur la meilleure opportunité trouvée
        if best_asset_score > 0:
            score += best_asset_score * 0.5  # Multiplicateur d'impact
            print(
                f"🎯 [SCALPING] Meilleure opportunité: {best_asset} (score: {best_asset_score:.3f}) -> +{best_asset_score * 0.5:.3f}"
            )

        # Bonus pour multiple opportunités
        if scalping_opportunities > 1:
            multi_opportunity_bonus = min(0.2, scalping_opportunities * 0.05)
            score += multi_opportunity_bonus
            print(
                f"📊 [SCALPING] {scalping_opportunities} opportunités détectées -> +{multi_opportunity_bonus:.3f}"
            )

        # Pénalités si conditions globales défavorables
        penalty = self._calculate_scalping_penalties(context)
        score -= penalty
        if penalty > 0:
            print(f"⚠️ [SCALPING] Pénalités appliquées: -{penalty:.3f}")

            return score

    def _evaluate_scalping_asset_conditions(
        self, asset: str, signals: Dict, config: Dict
    ) -> float:
        """
        🎯 ÉVALUATION CONDITIONS SCALPING PAR ASSET (version améliorée)
        - Volatilité : lecture en % avec fallback (décimal -> %)
        - Spread : fallback robuste via MT5Connector si 'spread' ou 'current_spread_points' sont absents/0
        - Volume : adaptation auto en basse volatilité globale
        - MTF : possibilité d'exiger l'alignement M5/M15 (EMA20 vs EMA50) si la clé 'mtf_ema_align' est fournie
        """
        import math

        if not signals or not isinstance(signals, dict):
            return 0.0

        condition_score = 0.0
        print(f"  🔍 [{asset}] Analyse conditions scalping...")

        # === 1) KATANA (inchangé) ===
        katana_signals = {
            "bos_mss_detected": 0.25,
            "volume_anomaly_detected": 0.25,
            "fvg_detected": 0.15,
            "liquidity_grab_detected": 0.20,
            "ob_detected": 0.20,
        }
        katana_score = 0.0
        detected_katana = []
        for signal, weight in katana_signals.items():
            if signals.get(signal, False):
                katana_score += weight
                detected_katana.append(signal)
        condition_score += katana_score
        if detected_katana:
            print(f"    🗡️ Signaux KATANA: {detected_katana} -> +{katana_score:.3f}")

        # === 2) CONDITIONS MTF ===
        # 2.1 Volatilité (%)
        vol_pct = None
        if isinstance(signals.get("volatility_pct"), (int, float)):
            vol_pct = float(signals["volatility_pct"])
        elif isinstance(signals.get("volatility_percentage"), (int, float)):
            vol_pct = float(signals["volatility_percentage"])
        else:
            vol_raw = signals.get("volatility")
            if isinstance(vol_raw, (int, float)):
                vol_raw = float(vol_raw)
                vol_pct = vol_raw * 100.0 if vol_raw <= 1.0 else vol_raw
            else:
                vol_pct = 0.0

        # 2.2 Spread (points) — avec fallback robuste (ask-bid)/point via MT5Connector
        spread_points = signals.get(
            "current_spread_points", signals.get("spread", None)
        )
        try:
            spread_points = float(spread_points)
        except Exception:
            spread_points = None

        if (
            spread_points is None
            or not math.isfinite(spread_points)
            or spread_points <= 0.0
        ):
            mt5c = getattr(self.config_manager, "mt5_connector", None)
            if mt5c:
                try:
                    spread_points = float(mt5c.get_symbol_spread_points(asset))
                    print(
                        f"    ℹ️ Spread (fallback MT5Connector): {spread_points:.0f} points"
                    )
                except Exception:
                    spread_points = float("inf")
            else:
                spread_points = float("inf")

        # 2.3 Volume z-score
        try:
            volume_zscore = float(signals.get("volume_zscore", 0.0))
        except Exception:
            volume_zscore = 0.0

        # 2.4 Seuils MTF depuis la conf (prod_config en priorité)
        volatility_threshold_pct = float(
            self.config_manager.get(
                "scoring_rules.scalping.mtf.min_volatility_pct", 0.01
            )
        )
        max_spread_points = float(
            self.config_manager.get("scoring_rules.scalping.mtf.max_spread_points", 50)
        )
        min_volume_zscore = float(
            self.config_manager.get("scoring_rules.scalping.mtf.min_volume_zscore", 0.5)
        )

        # Adaptation auto du seuil volume en basse volatilité
        global_vol_pct = signals.get("global_volatility_pct")
        if not isinstance(global_vol_pct, (int, float)):
            global_vol_pct = float(
                self.config_manager.get("last_computed_global_volatility_pct", 0.0)
                or 0.0
            )
        low_vol_th = float(
            self.config_manager.get(
                "adaptation_settings.volatility_thresholds.low", 0.05
            )
        )
        if global_vol_pct < low_vol_th:
            min_volume_zscore = float(
                self.config_manager.get(
                    "adaptation_settings.scalping.min_volume_zscore_low_vol",
                    min_volume_zscore,
                )
            )
            print(
                f"    🪶 Basse volatilité globale ({global_vol_pct:.2f}% < {low_vol_th:.2f}%) → seuil volume={min_volume_zscore}"
            )

        # 2.5 Alignement directionnel MTF (optionnel, si fourni par les signaux)
        require_align = bool(
            self.config_manager.get(
                "scoring_rules.scalping.mtf.require_directional_alignment", True
            )
        )
        mtf_align_val = signals.get("mtf_ema_align", None)  # attendu bool si présent

        mtf_conditions_met = 0
        mtf_total_conditions = 3  # vol, spread, volume

        # Volatilité
        if vol_pct >= volatility_threshold_pct:
            mtf_conditions_met += 1
            print(
                f"    ✅ Volatilité OK: {vol_pct:.3f}% >= {volatility_threshold_pct:.3f}%"
            )
        else:
            print(
                f"    ❌ Volatilité faible: {vol_pct:.3f}% < {volatility_threshold_pct:.3f}%"
            )

        # Spread
        if spread_points <= max_spread_points:
            mtf_conditions_met += 1
            print(f"    ✅ Spread OK: {spread_points:.0f} <= {max_spread_points:.0f}")
        else:
            print(f"    ❌ Spread élevé: {spread_points:.0f} > {max_spread_points:.0f}")

        # Volume
        if volume_zscore >= min_volume_zscore:
            mtf_conditions_met += 1
            print(f"    ✅ Volume OK: {volume_zscore:.2f} >= {min_volume_zscore:.2f}")
        else:
            print(
                f"    ❌ Volume faible: {volume_zscore:.2f} < {min_volume_zscore:.2f}"
            )

        # Alignement MTF (ne compte que si la clé est présente)
        if require_align and isinstance(mtf_align_val, bool):
            mtf_total_conditions += 1
            if mtf_align_val:
                mtf_conditions_met += 1
                print("    ✅ MTF aligné (M5 & M15)")
            else:
                print("    ❌ MTF non aligné (M5 & M15)")
        elif require_align and mtf_align_val is None:
            print(
                "    🟡 Alignement MTF non fourni dans 'signals' → ignoré (pas de pénalité)"
            )

        # Bonus proportionnel MTF
        mtf_score = (mtf_conditions_met / mtf_total_conditions) * 0.3
        condition_score += mtf_score
        print(
            f"    🔄 Conditions MTF: {mtf_conditions_met}/{mtf_total_conditions} -> +{mtf_score:.3f}"
        )

        # === 3) PHASES SCALPING (inchangé) ===
        current_phase = signals.get("phase", "")
        scalping_phases = {
            "scalp_burst_up": 1.0,
            "scalp_burst_down": 1.0,
            "expansion_up": 0.8,
            "expansion_down": 0.8,
            "micro_phase": 0.7,
            "trending_bullish": 0.6,
            "trending_bearish": 0.6,
        }
        phase_score = 0.0
        for phase_pattern, weight in scalping_phases.items():
            if phase_pattern in current_phase:
                phase_score = weight * 0.25  # 25% du score
                print(f"    ⚡ Phase scalping: {current_phase} -> +{phase_score:.3f}")
                break
        condition_score += phase_score

        # === 4) QUALITÉ & CONFIANCE (inchangé) ===
        confidence = signals.get("confidence_score", 0.0)
        is_liquid = signals.get("is_liquid", False)

        if confidence > 0.7:
            condition_score += 0.15
            print(f"    📈 Haute confiance: {confidence:.3f} -> +0.150")
        elif confidence > 0.5:
            condition_score += 0.05
            print(f"    📊 Confiance OK: {confidence:.3f} -> +0.050")

        if is_liquid:
            condition_score += 0.10
            print(f"    💧 Asset liquide -> +0.100")

        # === 5) CONFLUENCE MTF (inchangé) ===
        multi_tf_enabled = signals.get("multi_tf_enabled", False)
        confluence_score = signals.get("confluence_score", 0.0)

        if multi_tf_enabled and confluence_score > 0.7:
            condition_score += 0.20
            print(f"    🔥 MTF Confluence élevée: {confluence_score:.3f} -> +0.200")
        elif multi_tf_enabled and confluence_score > 0.5:
            condition_score += 0.10
            print(f"    🔄 MTF Confluence OK: {confluence_score:.3f} -> +0.100")

        final_score = min(1.0, condition_score)
        print(f"  🎯 [{asset}] Score final: {final_score:.3f}")
        return final_score

    def _calculate_scalping_penalties(self, context: Dict) -> float:
        """
        ⚠️ PÉNALITÉS SCALPING

        Conditions globales défavorables au scalping
        """
        penalty = 0.0

        # Pénalité si marché fermé ou illiquide globalement
        market_state = context.get("market_state", "open")
        if market_state != "open":
            penalty += 0.3
            print(f"⚠️ [PENALTY] Marché fermé: +0.3")

        # Pénalité si volatilité globale trop faible
        global_volatility = context.get("market_volatility_percentage", 0.0)
        if global_volatility < 10.0:  # Seuil de volatilité minimale
            penalty += 0.2
            print(
                f"⚠️ [PENALTY] Volatilité globale faible ({global_volatility:.1f}%): +0.2"
            )

        # Pénalité si actualités majeures
        if context.get("major_news_active", False):
            penalty += 0.25
            print(f"⚠️ [PENALTY] Actualités majeures: +0.25")

        return penalty

    def select_optimal_config(
        self, context: Dict[str, Any], configs: Dict[str, Any]
    ) -> Dict[str, Any]:
        """
        Sélectionne la configuration optimale en se basant sur un scoring sophistiqué.
        Déplacée de ConfigManager.
        """
        self.logger.info("Sélection de la configuration optimale...")

        scored_configs = self.score_configs(context, configs)

        optimal_config_path = None
        optimal_score = -1.0

        if scored_configs:
            sorted_configs = sorted(
                scored_configs.items(), key=lambda item: item[1], reverse=True
            )
            optimal_config_path, optimal_score = sorted_configs[0]

            min_optimal_score_threshold = self.config_manager.get(
                "scoring_rules.min_optimal_score_threshold", 0.1
            )
            if optimal_score < min_optimal_score_threshold:
                self.logger.warning(
                    f"Le score optimal ({optimal_score:.2f}) est inférieur au seuil minimal ({min_optimal_score_threshold:.2f}). Cherche une stratégie par défaut."
                )
                optimal_config_path = None

        if optimal_config_path is None:
            self.logger.warning(
                "Aucune configuration optimale trouvée par le scoring ou le score est trop bas. Tentative de chargement de la stratégie par défaut."
            )

            default_strategy_key = self.config_manager.get(
                "strategies.default_strategy"
            )
            if default_strategy_key:
                config_mapping = self.config_manager.get(
                    "strategies.config_mapping", {}
                )
                default_strategy_file_name = config_mapping.get(default_strategy_key)
                if default_strategy_file_name:
                    actual_config_dir = Path(
                        self.config_manager.get("paths.strategy_configs", "config/")
                    )
                    default_strategy_file_path = (
                        actual_config_dir / default_strategy_file_name
                    )

                    try:
                        # Utilise ConfigLoader pour parser
                        default_strategy_content = (
                            self.config_manager.config_loader.parse_json_config(
                                str(default_strategy_file_path)
                            )
                        )
                        # Ensure consistent structure
                        if (
                            isinstance(default_strategy_content, dict)
                            and "content" not in default_strategy_content
                        ):
                            optimal_config_content = default_strategy_content
                        else:
                            optimal_config_content = default_strategy_content.get(
                                "content", default_strategy_content
                            )
                        optimal_score = 0.0
                        self.logger.info(
                            f"Stratégie par défaut '{default_strategy_key}' chargée comme fallback."
                        )

                    except Exception as e:
                        self.logger.error(
                            f"Échec du chargement de la stratégie par défaut '{default_strategy_key}' depuis {default_strategy_file_path}: {e}",
                            exc_info=True,
                        )
                        return {}
                else:
                    self.logger.error(
                        f"Aucun fichier de configuration mappé pour la stratégie par défaut '{default_strategy_key}'. Impossible de fournir un fallback."
                    )
                    return {}
            else:
                self.logger.critical(
                    "Aucune 'default_strategy' n'est définie et aucune optimale n'a été sélectionnée. Impossible de procéder."
                )
                return {}
        else:
            config_data = configs[optimal_config_path]
            optimal_config_content = config_data.get("config", config_data)

        self.logger.info(
            f"Configuration finale sélectionnée : '{optimal_config_content.get('strategy_name')}' avec un score de {optimal_score:.2f}"
        )

        market_regime = context.get("current_market_regime", "unknown_regime_fallback")

        self.config_manager.log_decision(  # Log via ConfigManager
            config=optimal_config_content,
            trade_decision={
                "action": "SELECT_STRATEGY",
                "strategy_name": optimal_config_content.get("strategy_name"),
                "score": optimal_score,
            },
            context=context,
            reason=f"Configuration optimale sélectionnée via scoring (score: {optimal_score:.2f}) pour le régime de marché '{market_regime}'",
        )
        return optimal_config_content

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
        Orchestrateur des sorties:
        1) Délègue aux stratégies actives (via magic number).
        2) Fallback générique si aucune stratégie n'est trouvée ou silencieuse:
            - Breakeven (SL -> entry +/- 0.1 pip) si PnL latent >= X*R
            - Trailing structurel sur dernier swing M1 en faveur
            - Time-stop après N bougies M1

        context['market_data'][symbol] DOIT contenir:
        - current_price (float) ou annotated_rates_df (DataFrame) pour récupérer last close
        - annotated_rates_df pour détecter swings/time-stop (si dispo)
        - symbol_info (digits/point)

        Sortie (liste de décisions génériques):
        - {"action": "MODIFY_SL", "position_id": ..., "symbol": ..., "new_sl": float, "reason": "breakeven|trail"}
        - {"action": "CLOSE", "position_id": ..., "symbol": ..., "close_volume": float, "reason": "time_stop"}
        """
        self.logger.info("Orchestration des sorties (stratégies + fallback)...")

        exit_decisions: List[Dict[str, Any]] = []
        if not open_positions:
            self.logger.debug("Aucune position ouverte.")
            return exit_decisions

    # ---- Helpers locaux robustes ----
    def _pos_id(p: Dict[str, Any]) -> Any:
        return p.get("ticket") or p.get("id") or p.get("Order") or p.get("Position") or p.get("position_id")

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

    def _bars_since_open(sym: str, mkt: Dict[str, Any], open_time: Optional[float]) -> Optional[int]:
        """
        Compte les bougies M1 écoulées depuis l'ouverture (si DF indexé en datetime).
        Fallback: None si impossible.
        """
        df = mkt.get

    
    def calculate_risk_parameters(self, context: dict, current_config: dict, trade_decision: dict) -> dict:
        """
        Calcule un dimensionnement 'risk-based' (lots), le RR, et applique des gardes simples.
        Signature alignée à l'appel existant: (context, current_config, trade_decision).
        Retour: dict { ok, volume, rr, risk_amount, notes, reason, entry_price, sl_price, tp_price }

        Hypothèses:
        - context["account_info"] contient equity/balance
        - context["market_data"][asset]["symbol_info"] contient trade_contract_size, point, digits, volume_* (MT5 SymbolInfo asdict)
        - trade_decision peut inclure entry_price/sl_price/tp_price (ex: via snapshot katana)
        """
        notes = []

        # --- 1) Entrées de base ---
        action = str(trade_decision.get("action", "")).upper()
        asset  = str(trade_decision.get("asset", "")).upper()

        if action not in {"BUY", "SELL"} or not asset:
            return {"ok": False, "reason": "invalid_action_or_asset"}

        md = (context.get("market_data") or {}).get(asset, {}) or {}
        symbol_info = md.get("symbol_info", {}) or {}     # dict (MT5 SymbolInfo -> _asdict())
        account_info = context.get("account_info", {}) or {}

        # entry/sl/tp: idéalement fournis par la décision; sinon entry=prix courant
        entry = trade_decision.get("entry_price", md.get("current_price"))
        sl    = trade_decision.get("sl_price")
        tp    = trade_decision.get("tp_price")

        # Casting robustes
        try:
            if entry is None:
                return {"ok": False, "reason": "missing_entry_price"}
            entry = float(entry)
            sl = None if sl is None else float(sl)
            tp = None if tp is None else float(tp)
        except (TypeError, ValueError):
            return {"ok": False, "reason": "invalid_level_types"}

        # --- 2) Paramètres broker/symbole (avec defaults sûrs) ---
        contract   = float(symbol_info.get("trade_contract_size", 100000.0)) or 100000.0
        point      = float(symbol_info.get("point", 0.00001)) or 0.00001
        digits     = int(symbol_info.get("digits", 5))
        vol_min    = float(symbol_info.get("volume_min", 0.01)) or 0.01
        vol_max    = float(symbol_info.get("volume_max", 100.0)) or 100.0
        vol_step   = float(symbol_info.get("volume_step", 0.01)) or 0.01
        spread_pts = float(md.get("current_spread_points", 0.0)) or 0.0

        # --- 3) Paramètres de risque (config) ---
        rm_cfg            = (current_config or {}).get("risk_management", {}) or {}
        risk_pct          = float(rm_cfg.get("risk_per_trade_pct", 0.5))   # % de l'equity
        min_rr            = float(rm_cfg.get("min_rr", 1.2))
        max_spread_pips   = float(rm_cfg.get("max_spread_pips", 2.0))      # garde simple (scalping)
        fixed_volume_lots = rm_cfg.get("fixed_volume_lots")                 # fallback si pas de SL/TP

        equity = float(account_info.get("equity", account_info.get("balance", 0.0)) or 0.0)
        if equity <= 0:
            return {"ok": False, "reason": "no_equity"}

        # --- 4) Conversion spread points -> pips (approx) ---
        # MT5: 'spread' exprimé en points (unités de 'point').
        # Convention simple: pour 5/3 digits => 1 pip = 10 points; sinon ~1 point = 1 pip (fallback).
        pip_points = 10.0 if digits in (3, 5) else 1.0
        spread_pips = spread_pts / pip_points

        # --- 5) Pas de niveaux -> fallback volume fixe (ou min) ---
        if sl is None or tp is None or sl == entry:
            if fixed_volume_lots is None:
                fixed_volume_lots = max(vol_min, vol_step)
                notes.append("fallback_fixed_volume_min")
            else:
                try:
                    fixed_volume_lots = float(fixed_volume_lots)
                except (TypeError, ValueError):
                    fixed_volume_lots = max(vol_min, vol_step)
                    notes.append("fallback_fixed_volume_min_parse_error")

            if spread_pips > max_spread_pips:
                return {"ok": False, "reason": f"spread_too_wide_{spread_pips:.2f}p"}

            return {
                "ok": True,
                "volume": self._quantize_volume(fixed_volume_lots, vol_min, vol_max, vol_step),
                "rr": None,
                "risk_amount": equity * (risk_pct / 100.0),
                "notes": ["no_levels_for_risk_sizing"] + notes,
                "entry_price": entry,
                "sl_price": sl,
                "tp_price": tp,
            }

        # --- 6) Sizing au risque (avec niveaux valides) ---
        sl_dist = abs(entry - sl)
        if sl_dist <= 0:
            return {"ok": False, "reason": "invalid_sl_distance"}

        risk_amount = equity * (risk_pct / 100.0)

        # Perte par lot à SL ≈ sl_dist * contract  (voir commentaire dans ta version)
        try:
            raw_volume = risk_amount / (sl_dist * contract)
        except ZeroDivisionError:
            return {"ok": False, "reason": "invalid_contract_or_sl_dist"}

        volume = self._quantize_volume(raw_volume, vol_min, vol_max, vol_step)

        # --- 7) RR & gardes simples ---
        rr = (abs(tp - entry) / sl_dist) if sl_dist > 0 else 0.0
        if rr < min_rr:
            rr_fmt = f"{rr:.2f}"; min_rr_fmt = f"{min_rr:.2f}"
            return {"ok": False, "reason": f"rr_below_min_{rr_fmt}_<{min_rr_fmt}"}

        if spread_pips > max_spread_pips:
            return {"ok": False, "reason": f"spread_too_wide_{spread_pips:.2f}p"}

        return {
            "ok": True,
            "volume": volume,
            "rr": rr,
            "risk_amount": risk_amount,
            "notes": notes,
            "entry_price": entry,
            "sl_price": sl,
            "tp_price": tp,
        }


    def _quantize_volume(self, vol: float, vmin: float, vmax: float, vstep: float) -> float:
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

        Returns:
            Dict: La décision de trade (dict) ou {} si aucune opportunité valide.
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
                get_tracker_from_context(context).note(asset_sym, "sizing", reason, extra or {})
            except Exception:
                pass

        self.logger.info("CORE DECISION ENGINE - Prise de décision directe sans délégation...")
        self.logger.debug(f"Signaux reçus pour évaluation: {signals}")

        # 1) Filtres pré-décision critiques (sécurité globale)
        if current_config.get("halt_on_major_news", True) and self.config_manager.check_news_schedule(
            context, context.get("economic_calendar", [])
        ):
            self.logger.warning("Trade suspendu en raison d'un événement d'actualité majeur.")
            self.config_manager.log_decision(current_config, {}, context, "Trade bloqué: Actualité majeure.")
            return {}

        # 2) Récupérer le nom de stratégie
        strategy_name = current_config.get("strategy_name", "unknown")
        self.logger.info(f"🎯 CORE prend la décision avec paramètres de stratégie: {strategy_name}")

        # 3) CORE évalue directement les signaux (sans délégation)
        trade_decision = self._core_evaluate_signals(context, current_config, signals, strategy_name)

        if not trade_decision:
            self.logger.info(f"CORE n'a trouvé aucune opportunité d'entrée ce cycle avec les paramètres '{strategy_name}'.")
            return {}

        # --- 🔒 Normalisation/Validation ACTION & ASSET (anti-UNKNOWN) ---
        action_raw = str(trade_decision.get("action", "")).upper()
        action_map = {"LONG": "BUY", "SHORT": "SELL", "BUY": "BUY", "SELL": "SELL", "CLOSE": "CLOSE"}
        normalized_action = action_map.get(action_raw)

        if not normalized_action:
            self.logger.warning(f"Action inconnue '{action_raw}' depuis core_evaluate_signals -> décision ignorée proprement.")
            self.config_manager.log_decision(current_config, {}, context, f"Décision ignorée (action inconnue: {action_raw})")
            return {}

        asset_raw = str(trade_decision.get("asset", "")).upper().strip()
        if not asset_raw:
            self.logger.warning("Décision reçue sans 'asset' -> décision ignorée.")
            self.config_manager.log_decision(current_config, {}, context, "Décision ignorée (asset vide).")
            return {}

        allowed_assets = set(map(str.upper, current_config.get("tradeable_assets", [])))
        if allowed_assets and asset_raw not in allowed_assets:
            self.logger.warning(
                f"Asset '{asset_raw}' non autorisé pour la stratégie '{strategy_name}'. Whitelist: {sorted(allowed_assets)}"
            )
            self.config_manager.log_decision(current_config, {}, context, f"Décision ignorée (asset non autorisé: {asset_raw})")
            return {}

        order_type = str(trade_decision.get("order_type", "MARKET")).upper()
        if order_type not in {"MARKET", "BUY_LIMIT", "SELL_LIMIT", "BUY_STOP", "SELL_STOP"}:
            self.logger.debug(f"order_type inconnu '{order_type}', fallback 'MARKET'.")
            order_type = "MARKET"

        trade_decision["action"] = normalized_action
        trade_decision["asset"] = asset_raw
        trade_decision["order_type"] = order_type

        # 4) Contrôles compte/risque simples côté pipeline (pas d'exception)
        active_broker_account = context.get("active_broker_account", {})
        max_positions_for_account = active_broker_account.get("trade_settings", {}).get("max_open_positions", 999)
        current_open_positions = context.get("open_positions", [])

        self.logger.debug(f"Positions ouvertes actuelles: {len(current_open_positions)} / Max: {max_positions_for_account}")

        if len(current_open_positions) >= max_positions_for_account:
            self.logger.warning(
                f"Trade bloqué: Max positions ({max_positions_for_account}) atteint pour le compte {active_broker_account.get('account_id')}."
            )
            return {}

        # 5) Sizing au risque — instrumenté DIAG
        risk_params = self.calculate_risk_parameters(context, current_config, trade_decision)
        self.logger.debug(f"Paramètres de risque calculés: {risk_params}")

        if not risk_params or not bool(risk_params.get("ok", False)):
            reason = (risk_params or {}).get("reason", "risk_calc_failed")
            extras = {k: risk_params.get(k) for k in ("sl_pips", "tp_pips", "spread_pips", "rr_effective", "stops_level_pips", "level_mode") if isinstance(risk_params, dict) and k in risk_params}
            _diag_size(asset_raw, reason, extras)
            self.logger.warning(f"Calcul de risque refusé pour {asset_raw}: {reason}")
            return {}

        if not (risk_params.get("volume", 0.0) > 0):
            _diag_size(asset_raw, "sizing_volume_zero_or_missing", {"ok": True, "volume": risk_params.get("volume")})
            self.logger.warning("Calcul de risque valide mais volume nul/invalide. Trade annulé.")
            return {}

        trade_decision.update(risk_params)

        # Log final
        self.config_manager.log_decision(
            current_config,
            trade_decision,
            context,
            f"Décision CORE avec paramètres '{strategy_name}': {trade_decision.get('rule_name', 'N/A')}",
        )
        return trade_decision


    def _core_evaluate_signals(
        self,
        context: Dict[str, Any],
        config: Dict[str, Any],
        signals: Dict[str, Any],
        strategy_name: str,
    ) -> Dict[str, Any]:
        """
        CORE évalue directement les signaux et prend la décision finale.

        Mode SCALPING KATANA :
        - Pas de break M1 obligatoire.
        - On valide si au moins un déclencheur est présent (break/BOS/FVG/OB) ou règle "2 sur 3".
        - Qualité minimale : confiance, spread, ema_spread (+ align MTF optionnel).
        """

        self.logger.info(f"🔍 CORE analyse {len(signals)} assets avec paramètres {strategy_name}")

        # ------- Détection des modes -------
        strat = str(strategy_name or "").lower()
        is_scalping = ("scalping" in strat)
        gating_mode_cfg = str(self.config_manager.get("entry_rules.scalping.gating_mode", "normal")).lower()
        is_katana = is_scalping and (gating_mode_cfg == "katana" or "katana" in strat)

        # ------- Seuils généraux -------
        min_confidence_default = float(config.get("min_confidence", 0.65))

        # ------- Seuils SCALPING communs -------
        require_align_default = bool(self.config_manager.get("entry_rules.scalping.require_mtf_align", True))
        require_m1_break_default = bool(self.config_manager.get("entry_rules.scalping.require_m1_break", True))
        max_spread_pts = float(self.config_manager.get("entry_rules.scalping.max_spread_points", 50))
        min_m1_ema_spread = float(self.config_manager.get("entry_rules.scalping.min_m1_ema_spread", 0.0))
        min_confidence_scalp = float(self.config_manager.get("entry_rules.scalping.min_confidence", min_confidence_default))
        fvg_max = float(self.config_manager.get("entry_rules.scalping.fvg_max_distance_pips", 2.0) or 2.0)
        ob_max  = float(self.config_manager.get("entry_rules.scalping.ob_max_distance_pips",  2.0) or 2.0)

        # ------- Seuils KATANA spécifiques (relax) -------
        require_align_katana = bool(self.config_manager.get("entry_rules.scalping.katana_require_mtf_align", False))
        # confiance un peu plus souple en katana (sinon utiliser le même seuil)
        min_confidence_katana = float(self.config_manager.get("entry_rules.scalping.katana_min_confidence",
                                                            max(0.35, min_confidence_scalp * 0.8)))
        # "proximité douce" optionnelle (léger coussin)
        soft_mult = float(self.config_manager.get("entry_rules.scalping.katana_soft_distance_mult", 1.25))
        fvg_soft = fvg_max * soft_mult
        ob_soft  = ob_max * soft_mult

        best_asset, best_score, best_signals = None, -1.0, None

        for asset, asset_signals in signals.items():
            confidence = float(asset_signals.get("confidence_score", 0.0))
            phase = str(asset_signals.get("phase", ""))

            # Score de classement (simple)
            score = confidence
            if asset_signals.get("ob_detected", False):
                score += 0.10
            if asset_signals.get("fvg_detected", False):
                score += 0.10
            if asset_signals.get("bos_mss_detected", False):
                score += 0.15

            # === Comportement non-scalping : permissif comme avant ===
            if not is_scalping:
                is_valid = False
                if confidence >= min_confidence_default:
                    is_valid = True
                    self.logger.info(
                        f"✅ [{asset}] Accepté par CORE (non-scalping) - Confiance {confidence:.3f} >= {min_confidence_default}"
                    )
                elif confidence >= 0.5 and any(k in phase.lower() for k in ["bullish", "bearish", "trending"]):
                    is_valid = True
                    self.logger.info(f"✅ [{asset}] Accepté par CORE (non-scalping) - Phase conclusive: {phase}")

                if is_valid and score > best_score:
                    best_asset, best_score, best_signals = asset, score, asset_signals
                continue

            # === Lecture MTF / micro-timing / marchés ===
            mtf_align_val = asset_signals.get("mtf_ema_align", None)  # bool si dispo
            mtf_direction = str(asset_signals.get("mtf_direction", "none")).lower()

            # spread en points
            spread_points = asset_signals.get("current_spread_points", asset_signals.get("spread", float("inf")))
            try:
                spread_points = float(spread_points)
            except Exception:
                spread_points = float("inf")

            m1_ema_spread = float(asset_signals.get("m1_ema_spread", 0.0))
            m1_hh_break = bool(asset_signals.get("m1_last_hh_break", False))
            m1_ll_break = bool(asset_signals.get("m1_last_ll_break", False))

            # Déclencheurs (BOS/FVG/OB)
            bos = asset_signals.get("bos_mss_details") or {}
            fvg = asset_signals.get("fvg_details") or {}
            ob  = asset_signals.get("ob_details")  or {}

            bos_ok = bool(bos.get("confirmed") or bos.get("is_confirmed") or asset_signals.get("bos_mss_detected"))
            fvg_dist = float(fvg.get("distance_pips") or asset_signals.get("fvg_distance_pips") or 1e9)
            ob_dist  = float(ob.get("distance_pips")  or asset_signals.get("ob_distance_pips")  or 1e9)
            ob_valid = bool(ob.get("validated") or ob.get("valid") or asset_signals.get("ob_detected"))

            fvg_ok_strict = (fvg_dist <= fvg_max)
            ob_ok_strict  = (ob_valid and ob_dist <= ob_max)

            # Break M1 dans le sens
            if mtf_direction == "up":
                m1_break = m1_hh_break
            elif mtf_direction == "down":
                m1_break = m1_ll_break
            else:
                m1_break = False

            # “2 sur 3” : {break M1, BOS confirmé, (FVG proche OU OB proche)}
            two_of_three = (int(bool(m1_break)) + int(bool(bos_ok)) + int(bool(fvg_ok_strict or ob_ok_strict))) >= 2

            # Qualité commune
            confidence_ok_scalp = confidence >= min_confidence_scalp
            spread_ok     = spread_points <= max_spread_pts
            ema_spread_ok = m1_ema_spread >= min_m1_ema_spread

            # --------------------------------
            # MODE KATANA (relax)
            # --------------------------------
            if is_katana:
                # Alignement MTF optionnel
                align_ok = True
                if require_align_katana:
                    if isinstance(mtf_align_val, bool):
                        align_ok = (mtf_align_val is True)
                    else:
                        align_ok = False

                # Déclencheurs katana : on autorise “proximité douce”
                fvg_ok_katana = fvg_dist <= fvg_soft
                ob_ok_katana  = (ob_valid and ob_dist <= ob_soft) or (ob_dist <= ob_max)  # très proche même non validé

                trigger_ok = (m1_break or bos_ok or fvg_ok_katana or ob_ok_katana or two_of_three)
                conf_ok = (confidence >= min_confidence_katana)

                is_valid = conf_ok and spread_ok and ema_spread_ok and align_ok and trigger_ok

                if not is_valid:
                    # LOGS explicites
                    if not conf_ok:
                        self.logger.info(f"❌ [{asset}] rejeté (scalping/katana): confiance {confidence:.3f} < {min_confidence_katana:.3f}")
                    if not spread_ok:
                        self.logger.info(f"❌ [{asset}] rejeté (scalping/katana): spread {spread_points:.1f} > {max_spread_pts}")
                    if not ema_spread_ok:
                        self.logger.info(f"❌ [{asset}] rejeté (scalping/katana): m1_ema_spread {m1_ema_spread:.5f} < {min_m1_ema_spread:.5f}")
                    if require_align_katana and not align_ok:
                        self.logger.info(f"❌ [{asset}] rejeté (scalping/katana): MTF non aligné (M5 & M15)")
                    if not trigger_ok:
                        self.logger.info(f"· {asset} → core_gate:gate_katana_failed")
                    continue

                self.logger.info(f"· {asset} → core_gate:gate_katana_ok")
                self.logger.info(f"✅ [{asset}] Accepté par CORE (scalping/katana)")
                if score > best_score:
                    best_asset, best_score, best_signals = asset, score, asset_signals
                continue

            # --------------------------------
            # MODE SCALPING (normal/aggressive existants)
            # --------------------------------
            require_align = require_align_default
            require_m1_break = require_m1_break_default
            allow_alt_without_break = bool(self.config_manager.get("entry_rules.scalping.allow_alt_without_break", True))

            # Alignement MTF si requis
            align_ok = True
            if require_align:
                if isinstance(mtf_align_val, bool):
                    align_ok = (mtf_align_val is True)
                else:
                    align_ok = False  # info absente → refuse si exigé

            # Micro-timing (break demandé uniquement si exigé ET si pas d'alternatives autorisées)
            m1_break_ok = True
            if require_m1_break:
                m1_break_ok = bool(m1_break)

            alt_trigger_ok = (bos_ok and fvg_ok_strict) or ob_ok_strict
            entry_gate_ok = (m1_break_ok if require_m1_break else True)
            if (not entry_gate_ok) and allow_alt_without_break and alt_trigger_ok:
                entry_gate_ok = True

            confidence_ok = confidence_ok_scalp
            is_valid = confidence_ok and spread_ok and ema_spread_ok and align_ok and entry_gate_ok

            if not is_valid:
                if not confidence_ok:
                    self.logger.info(f"❌ [{asset}] rejeté (scalping): confiance {confidence:.3f} < {min_confidence_scalp:.3f}")
                if not spread_ok:
                    self.logger.info(f"❌ [{asset}] rejeté (scalping): spread {spread_points:.1f} > {max_spread_pts}")
                if not ema_spread_ok:
                    self.logger.info(f"❌ [{asset}] rejeté (scalping): m1_ema_spread {m1_ema_spread:.5f} < {min_m1_ema_spread:.5f}")
                if require_align and not align_ok:
                    self.logger.info(f"❌ [{asset}] rejeté (scalping): MTF non aligné (M5 & M15)")
                if require_m1_break and not entry_gate_ok:
                    if allow_alt_without_break:
                        self.logger.info(
                            f"❌ [{asset}] rejeté (scalping): pas de break M1 dans le sens ({mtf_direction}) et "
                            f"aucun déclencheur alternatif (BOS/FVG/OB) proche"
                        )
                    else:
                        self.logger.info(f"❌ [{asset}] rejeté (scalping): break M1 exigé et absent ({mtf_direction})")
                self.logger.info(f"· {asset} → core_gate:gate_normal_failed" if gating_mode_cfg == "normal" else
                                f"· {asset} → core_gate:gate_aggressive_failed")
                continue

            self.logger.info(f"· {asset} → core_gate:gate_normal_ok" if gating_mode_cfg == "normal"
                            else f"· {asset} → core_gate:gate_aggressive_ok")
            self.logger.info(f"✅ [{asset}] Accepté par CORE (scalping)")
            if score > best_score:
                best_asset, best_score, best_signals = asset, score, asset_signals

        if not best_asset:
            self.logger.info("❌ CORE: Aucun asset ne respecte les critères d'entrée")
            return {}

        self.logger.info(f"🎯 CORE sélectionne: {best_asset} (score: {best_score:.3f})")
        return self._core_build_trade_decision(best_asset, best_signals, config, context)

    def _core_build_trade_decision(
        self,
        asset: str,
        signals: Dict[str, Any],
        config: Dict[str, Any],
        context: Dict[str, Any],
    ) -> Dict[str, Any]:
        """
        CORE construit la décision finale de trade basée sur les signaux.

        🔒 Version SANS fallback momentum.
        - Direction priorisée par MTF si dispo, sinon phase (jamais momentum)
        - Gating micro-phase contrôlé par la config:
            * strict    : break M1 + retest obligatoires
            * normal    : strict OU (BOS confirmé + FVG proche) OU (OB validé proche)
            * aggressive: normal OU (break seul + spread & ATR OK)
        - Spread: seuils lus depuis la config (pips OU points), pas de valeur codée en dur
        - SL/TP dynamiques conservés (ATR/volatilité/spread) + garde-fous scalping
        - decision_trace + gates cohérents avec pre_trade_checks
        """
        from datetime import datetime, timezone
        # DIAG local pour tracer les refus/acceptations côté CORE
        try:
            from core.diagnostics import get_tracker_from_context
            def _diag(reason: str, extra: dict | None = None):
                try:
                    get_tracker_from_context(context).note(asset, "core_gate", reason, extra or {})
                except Exception:
                    pass
            def _diag_selected(rule: str, conf: float | None):
                try:
                    get_tracker_from_context(context).set_selected(asset, rule, conf)
                except Exception:
                    pass
        except Exception:
            def _diag(*a, **k):  # neutral fallback
                pass
            def _diag_selected(*a, **k):
                pass

        # ---------- 0) Données de base ----------
        phase = str(signals.get("phase", "") or "").lower()
        current_price = float(signals.get("close", 0) or 0)
        if current_price <= 0:
            self.logger.error(f"❌ Prix actuel manquant ou invalide pour {asset}")
            _diag("invalid_price_or_close", {"close": signals.get("close")})
            return {}

        strategy_name = str(config.get("strategy_name", "")).lower() or "scalping"

        # ---------- 1) Paramètres symboles / conversions ----------
        point = float(signals.get("symbol_point_value") or 0.0)
        if point <= 0:
            point = float(self.config_manager.get("risk_management_settings.default_points_in_pip", 1e-5) or 1e-5)
        digits = int(signals.get("symbol_digits") or (5 if point <= 1e-5 else 3))
        points_per_pip = 10.0 if digits in (3, 5) else 1.0
        pip_size = point * points_per_pip

        # Spread courant (points -> pips)
        spread_points = float(signals.get("current_spread_points", signals.get("spread", 0)) or 0.0)
        try:
            spread_points = float(spread_points)
        except Exception:
            spread_points = 0.0
        spread_pips = max(0.0, spread_points / points_per_pip)

        # ---------- 2) Seuils (spread/ATR/gating) depuis la config ----------
        sr = self.config_manager.get("entry_rules.scalping", {}) or {}
        # Spread: priorité au seuil en pips, sinon conversion du seuil en points, sinon exécution policy
        if "max_spread_pips" in sr:
            max_spread_pips_allowed = float(sr.get("max_spread_pips", 1.5) or 1.5)
        else:
            max_spread_points_cfg = sr.get("max_spread_points", self.config_manager.get("execution_policy.max_spread_points"))
            max_spread_pips_allowed = float(max_spread_points_cfg) / points_per_pip if max_spread_points_cfg else 1.5

        gating_mode = str(sr.get("gating_mode", "normal")).lower()   # 'strict' | 'normal' | 'aggressive'
        min_atr_m1_pips = float(sr.get("min_atr_m1_pips", 0.8) or 0.0)
        fvg_max_distance_pips = float(sr.get("fvg_max_distance_pips", 2.0) or 2.0)
        ob_max_distance_pips  = float(sr.get("ob_max_distance_pips", 2.0) or 2.0)
        mtf_soft_override = bool(sr.get("mtf_mismatch_soft_override", True))

        # ---------- 3) Déterminer la direction (sans momentum) ----------
        action: Optional[str] = None
        mtf_dir = str(signals.get("mtf_direction", "none")).lower()

        # a) priorité MTF si dispo
        if strategy_name == "scalping" and mtf_dir in ("up", "down"):
            action = "BUY" if mtf_dir == "up" else "SELL"

        # b) sinon, phase directionnelle
        if action is None:
            if any(k in phase for k in ["bull", "up", "accumulation", "expansion", "trend"]):
                action = "BUY"
            elif any(k in phase for k in ["bear", "down", "distribution"]):
                action = "SELL"

        if action is None:
            self.logger.info(f"⛔ {asset}: pas de direction claire (phase={phase or 'empty'}, mtf={mtf_dir}).")
            _diag("no_direction", {"phase": phase, "mtf": mtf_dir})
            return {}
        action = action.upper()
        if action not in ("BUY", "SELL"):
            self.logger.warning(f"❌ Action invalide déterminée: {action}")
            _diag("invalid_action_determined", {"action": action})
            return {}

        # ---------- 4) Gating micro-phase (non-bloquant mais strictement défini) ----------
        m1_break  = bool(
            signals.get("m1_break_in_direction")
            or signals.get("break_m1_in_direction")
            or signals.get("m1_break")
            or signals.get("break_m1")
            or False
        )
        m1_retest = bool(
            signals.get("m1_retest_confirmation")
            or signals.get("retest_m1_confirmation")
            or signals.get("m1_retest")
            or False
        )
        bos = signals.get("bos_mss_details") or {}
        fvg = signals.get("fvg_details") or {}
        ob  = signals.get("ob_details")  or {}

        fvg_dist_pips = float(fvg.get("distance_pips") or 1e9)
        ob_dist_pips  = float(ob.get("distance_pips") or 1e9)
        bos_confirmed = bool(bos.get("confirmed") or bos.get("is_confirmed"))

        # ATR M1 (prix -> pips)
        atr_m1 = float(signals.get("atr_m1", 0.0) or 0.0)
        atr_m1_pips = (atr_m1 / pip_size) if pip_size > 0 else 0.0

        cond_strict  = (m1_break and m1_retest)
        cond_bos_fvg = (bos_confirmed and fvg_dist_pips <= fvg_max_distance_pips)
        cond_ob_near = (bool(ob.get("validated") or ob.get("valid")) and ob_dist_pips <= ob_max_distance_pips)
        cond_aggr    = (m1_break and (spread_pips <= max_spread_pips_allowed) and (atr_m1_pips >= max(0.0, 0.8 * min_atr_m1_pips)))

        if   gating_mode == "strict":
            gate_ok = cond_strict
        elif gating_mode == "normal":
            gate_ok = cond_strict or cond_bos_fvg or cond_ob_near
        else:  # aggressive
            gate_ok = cond_strict or cond_bos_fvg or cond_ob_near or cond_aggr

        if not gate_ok:
            self.logger.info(
                f"⛔ {asset}: gate_{gating_mode}_failed "
                f"(break={m1_break}, retest={m1_retest}, bos={bos_confirmed}, "
                f"fvg_dist={fvg_dist_pips:.2f}, ob_dist={ob_dist_pips:.2f})"
            )
            _diag(f"gate_{gating_mode}_failed", {
                "break": m1_break, "retest": m1_retest,
                "bos": bos_confirmed, "fvg_dist_pips": fvg_dist_pips, "ob_dist_pips": ob_dist_pips
            })
            return {}

        # ---------- 5) Alignement MTF souple ----------
        if mtf_dir in ("up", "down"):
            expected = "BUY" if mtf_dir == "up" else "SELL"
            if expected != action:
                if not mtf_soft_override:
                    self.logger.info(f"⛔ {asset}: MTF mismatch (mtf={mtf_dir}, action={action})")
                    _diag("mtf_mismatch", {"mtf": mtf_dir, "action": action})
                    return {}
                else:
                    self.logger.info(f"⚠️ {asset}: MTF mismatch toléré (override micro activé).")

        # ---------- 6) Garde-fous micro (ATR/Spread/SL cap) ----------
        # 6.a ATR M1 minimum
        atr_min = float(self.config_manager.get("entry_rules.scalping.min_atr_m1_pips", 0.0) or 0.0)
        if atr_min > 0 and atr_m1_pips < atr_min:
            self.logger.info(f"⛔ {asset}: ATR M1 trop faible ({atr_m1_pips:.2f} < {atr_min}).")
            _diag("atr_m1_too_low", {"atr_m1_pips": atr_m1_pips, "min": atr_min})
            return {}

        # 6.b Spread maximum (pips)
        if spread_pips > max_spread_pips_allowed:
            self.logger.info(f"⛔ {asset}: spread {spread_pips:.2f}p > {max_spread_pips_allowed:.2f}p autorisé.")
            _diag("spread_too_high", {"spread_pips": spread_pips, "max_allowed": max_spread_pips_allowed})
            return {}

        # ---------- 7) SL/TP dynamiques (ATR/vol/spread) ----------
        base_sl_pips = float(config.get("stop_loss_pips", 20) or 20)
        base_tp_pips = float(config.get("take_profit_pips", 40) or 40)
        magic_number = int(config.get("magic_number", 999_999))

        atr_m5 = float(signals.get("atr_m5", 0.0) or 0.0)
        atr_m5_pips = (atr_m5 / pip_size) if pip_size > 0 else 0.0

        # Volatilité %
        vol_pct = 0.0
        if isinstance(signals.get("volatility_pct"), (int, float)):
            vol_pct = float(signals["volatility_pct"])
        elif isinstance(signals.get("volatility_percentage"), (int, float)):
            vol_pct = float(signals["volatility_percentage"])
        else:
            vraw = signals.get("volatility")
            if isinstance(vraw, (int, float)):
                vraw = float(vraw)
                vol_pct = vraw * 100.0 if vraw <= 1.0 else vraw

        adapt = self.config_manager.get("adaptation_settings", {}) or {}
        vol_th = adapt.get("volatility_thresholds") or {}
        low_vol_th = float(vol_th.get("low", 0.05) or 0.05)
        high_vol_th = float(vol_th.get("high", 0.5) or 0.5)

        scalping_adapt = adapt.get("scalping") or {}
        sl_high = float(scalping_adapt.get("stop_loss_pips_high_vol", base_sl_pips))
        tp_high = float(scalping_adapt.get("take_profit_pips_high_vol", base_tp_pips))
        sl_low  = float(scalping_adapt.get("stop_loss_pips_low_vol",  base_sl_pips))
        tp_low  = float(scalping_adapt.get("take_profit_pips_low_vol",  base_tp_pips))

        if vol_pct >= high_vol_th:
            sl_pips = max(sl_high, atr_m5_pips * 0.8)
            tp_pips = tp_high
            regime_tag = "high_vol"
        elif vol_pct <= low_vol_th:
            sl_pips = max(sl_low, atr_m5_pips * 0.6)
            tp_pips = tp_low
            regime_tag = "low_vol"
        else:
            sl_pips = max(base_sl_pips, atr_m5_pips * 0.7)
            tp_pips = base_tp_pips
            regime_tag = "normal_vol"

        # Ajustements spread
        tp_pips = max(1.0, tp_pips - spread_pips)       # protège R:R effectif
        sl_pips = max(1.0, sl_pips + spread_pips * 0.5) # buffer anti-tap SL par spread

        # Cap SL micro-scalp
        sl_cap = float(self.config_manager.get("entry_rules.scalping.max_stop_pips_scalp", 0.0) or 0.0)
        reject_if_over_cap = bool(self.config_manager.get("entry_rules.scalping.reject_if_sl_over_cap", False))
        if sl_cap > 0 and sl_pips > sl_cap:
            if reject_if_over_cap:
                self.logger.info(f"⛔ {asset}: SL calculé {sl_pips:.2f}p > cap {sl_cap:.2f}p.")
                _diag("sl_over_cap", {"sl_pips": sl_pips, "cap": sl_cap})
                return {}
            sl_pips = sl_cap

        # ---------- 8) Decision trace + construction ----------
        decision_trace = {
            "phase": phase,
            "phase_m1": signals.get("phase_m1"),
            "phase_m5": signals.get("phase_m5"),
            "phase_m15": signals.get("phase_m15"),
            "mtf_direction": signals.get("mtf_direction"),
            "m1_break_in_direction": m1_break,
            "m1_retest_confirmation": m1_retest,
            "bos_mss_details": bos,
            "fvg_details": fvg,
            "ob_details": ob,
            "nearest_liquidity_level_details": signals.get("nearest_liquidity_level_details"),
            "atr_m1_pips": atr_m1_pips,
            "atr_m5_pips": atr_m5_pips,
            "spread_pips": spread_pips,
            "regime": regime_tag,
            "used_momentum_fallback": False,  # 🔒 désactivé pour de bon
        }
        gates = {
            "m1_break": m1_break,
            "m1_retest": m1_retest,
            "phase": phase,
            "gate_mode": gating_mode,
            "gate_passed": True,
            "gate_alt_bos_fvg": cond_bos_fvg,
            "gate_alt_ob": cond_ob_near,
        }

        timestamp = context.get("current_time_utc") or datetime.now(timezone.utc).isoformat()
        trade_decision = {
            "action": action,
            "asset": asset,
            "strategy_type": f"core_{config.get('strategy_name', 'decision')}",
            "entry_price": current_price,
            "target_sl_pips": float(round(sl_pips, 3)),
            "target_tp_pips": float(round(tp_pips, 3)),
            "rule_name": f"core_phase_scalp_{gating_mode}",
            "confidence": float(signals.get("confidence_score", 0.0) or 0.0),
            "timestamp": timestamp,
            "magic_number": int(config.get("magic_number", 999_999)),
            "decision_trace": decision_trace,
            "gates": gates,
            "meta": {
                "point": point,
                "points_per_pip": points_per_pip,
                "spread_points": spread_points,
                "spread_pips": spread_pips,
                "atr_m5": atr_m5,
                "atr_m5_pips": atr_m5_pips,
                "volatility_pct": vol_pct,
                "regime_tag": regime_tag,
                "max_spread_pips_allowed": max_spread_pips_allowed,
            },
        }

        self.logger.info(
            f"✅ CORE {action} {asset} @ {current_price} | "
            f"SL={trade_decision['target_sl_pips']}p, TP={trade_decision['target_tp_pips']}p "
            f"(gate={gating_mode}, spread={spread_pips:.2f}p≤{max_spread_pips_allowed:.2f}p, ATR_M5={atr_m5_pips:.2f}p)"
        )
        _diag_selected(trade_decision.get("rule_name"), trade_decision.get("confidence"))
        
        # ==== ENRICHISSEMENT POUR pre_trade_checks ====
        asset_signals   = asset_signals  # si ton param s'appelle autrement, adapte ici
        digits          = int(asset_signals.get("digits", 5) or 5)
        spread_pts_now  = float(asset_signals.get("current_spread_points", asset_signals.get("spread", 0.0)) or 0.0)
        mtf_direction   = str(asset_signals.get("mtf_direction", "none")).lower()
        phase           = str(asset_signals.get("phase", "")).lower()

        def _points_to_pips(d, pts):
            return float(pts) / (10.0 if d in (3, 5) else 1.0)

        gates = {
            "phase": phase,
            "m1_break":  bool(asset_signals.get("m1_last_hh_break") if mtf_direction == "up" else asset_signals.get("m1_last_ll_break")),
            "m1_retest": bool(asset_signals.get("m1_retest_confirmation")),
        }

        decision_trace = {
            "phase": phase,
            "mtf_direction": mtf_direction,
            "m1_break_in_direction": gates["m1_break"],
            "m1_retest_confirmation": gates["m1_retest"],
            "bos_mss_details": asset_signals.get("bos_mss_details", {}) or {},
            "fvg_details":     asset_signals.get("fvg_details",     {}) or {},
            "ob_details":      asset_signals.get("ob_details",      {}) or {},
            "atr_m1_pips":     float(asset_signals.get("atr_m1_pips", 0.0) or 0.0),
            "spread_pips":     _points_to_pips(digits, spread_pts_now),
            "used_momentum_fallback": False,
        }

        trade_decision["gates"] = gates
        trade_decision["decision_trace"] = decision_trace
        # ==== FIN ENRICHISSEMENT ====

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


    def calculate_risk_parameters(self, context: dict, current_config: dict, trade_decision: dict) -> dict:
        """
        Sizing au risque — compatible PRIX **ou** PIPS, version stricte "zéro hasard" pour scalping:
        - ❌ Refus si aucun niveau exploitable (ni prix sl/tp ni pips target_sl/target_tp)
        - ✅ Cohérence des niveaux (BUY: tp>entry>sl | SELL: tp<entry<sl)
        - ✅ SL borné par ATR (min/max multiples)
        - ✅ RR effectif (corrigé du spread) >= min_rr
        - ✅ Respect du stops_level broker (distance mini)
        - ✅ Bornes de risque (global_safety) + adaptation high_vol (si config présente)
        - Utilise le DF annoté: context['market_data'][asset]['annotated_rates_df']
        """
        notes = []

        # --- 1) Entrées de base ---
        action = str(trade_decision.get("action", "")).upper()
        asset  = str(trade_decision.get("asset", "")).upper()
        if action not in {"BUY", "SELL"} or not asset:
            return {"ok": False, "reason": "invalid_action_or_asset"}

        md = (context.get("market_data") or {}).get(asset, {}) or {}
        symbol_info = md.get("symbol_info", {}) or {}
        account_info = context.get("account_info", {}) or {}
        df = md.get("annotated_rates_df")

        entry = trade_decision.get("entry_price", md.get("current_price"))
        try:
            if entry is None:
                return {"ok": False, "reason": "missing_entry_price"}
            entry = float(entry)
        except (TypeError, ValueError):
            return {"ok": False, "reason": "invalid_entry_price"}

        # --- 2) Broker/symbole (unités et contraintes) ---
        contract   = float(symbol_info.get("trade_contract_size", 100000.0)) or 100000.0
        point      = float(symbol_info.get("point", 0.00001)) or 0.00001
        digits     = int(symbol_info.get("digits", 5))
        vol_min    = float(symbol_info.get("volume_min", 0.01)) or 0.01
        vol_max    = float(symbol_info.get("volume_max", 100.0)) or 100.0
        vol_step   = float(symbol_info.get("volume_step", 0.01)) or 0.01
        spread_pts = float(md.get("current_spread_points", 0.0)) or 0.0
        stops_lvl_points = float(symbol_info.get("stops_level", 0.0)) or 0.0

        # Pips (FX: 10 points = 1 pip pour digits 3/5)
        pip_points = 10.0 if digits in (3, 5) else 1.0
        pip_size   = point * pip_points
        spread_pips = spread_pts / pip_points
        stops_level_pips = stops_lvl_points / pip_points

        # --- 3) Risque (config) & adaptation ---
        rm_cfg   = (current_config or {}).get("risk_management", {}) or {}
        risk_pct = float(rm_cfg.get("risk_per_trade_pct", 0.25))
        min_rr   = float(rm_cfg.get("min_rr", 1.8))
        max_spread_pips_cfg = float(rm_cfg.get("max_spread_pips", 1.2))

        # Adaptation high_vol (si meta/regime_tag fourni)
        meta = trade_decision.get("meta", {}) or {}
        regime_tag = str(meta.get("regime_tag", "")).lower()
        adapt = (current_config or {}).get("adaptation_settings", {}).get("risk_adjustment", {}) or {}
        if regime_tag == "high_vol":
            mult = float(adapt.get("risk_reduction_multiplier_high_vol", 1.0) or 1.0)
            min_after = float(adapt.get("min_risk_percent_after_adjustment", 0.01) or 0.01)
            risk_pct = max(min_after, risk_pct * mult)

        # Cap global
        global_cap_pct = float(self.config_manager.get("global_safety.max_risk_per_trade_percent", 2.0) or 2.0)
        risk_pct = min(risk_pct, global_cap_pct)

        # Equity
        equity = float(account_info.get("equity", account_info.get("balance", 0.0)) or 0.0)
        if equity <= 0:
            return {"ok": False, "reason": "no_equity"}

        # --- 4) Niveaux: PRIX vs PIPS ---
        sl_price = trade_decision.get("sl_price")
        tp_price = trade_decision.get("tp_price")
        sl_pips_in  = trade_decision.get("target_sl_pips")
        tp_pips_in  = trade_decision.get("target_tp_pips")

        have_price_levels = (sl_price is not None) and (tp_price is not None)
        have_pip_levels   = (sl_pips_in is not None) and (tp_pips_in is not None)

        if not have_price_levels and not have_pip_levels:
            return {"ok": False, "reason": "missing_sl_or_tp_levels"}

        if have_price_levels:
            try:
                sl_price = float(sl_price); tp_price = float(tp_price)
            except (TypeError, ValueError):
                return {"ok": False, "reason": "invalid_level_types"}
            sl_dist_price = abs(entry - sl_price)
            tp_dist_price = abs(tp_price - entry)
            if sl_dist_price <= 0 or tp_dist_price <= 0:
                return {"ok": False, "reason": "invalid_distances_price"}
            sl_pips_val = sl_dist_price / pip_size
            tp_pips_val = tp_dist_price / pip_size

            # Cohérence directionnelle (PRIX)
            if action == "BUY" and not (tp_price > entry > sl_price):
                return {"ok": False, "reason": "levels_incoherent_for_buy"}
            if action == "SELL" and not (tp_price < entry < sl_price):
                return {"ok": False, "reason": "levels_incoherent_for_sell"}

            level_mode = "price"
        else:
            # Mode PIPS: construire des prix cohérents autour de entry
            try:
                sl_pips_val = float(sl_pips_in); tp_pips_val = float(tp_pips_in)
            except (TypeError, ValueError):
                return {"ok": False, "reason": "invalid_pip_types"}
            if sl_pips_val <= 0 or tp_pips_val <= 0:
                return {"ok": False, "reason": "invalid_distances_pips"}

            sl_dist_price = sl_pips_val * pip_size
            tp_dist_price = tp_pips_val * pip_size
            if action == "BUY":
                sl_price = entry - sl_dist_price
                tp_price = entry + tp_dist_price
            else:  # SELL
                sl_price = entry + sl_dist_price
                tp_price = entry - tp_dist_price

            level_mode = "pips"

        # --- 5) Bornes via ATR (si DF dispo) ---
        atr_settings = (rm_cfg.get("atr_settings") or {})
        slc = rm_cfg.get("sl_constraints", {}) or {}
        atr_period = int(atr_settings.get("period", 14))
        min_k = float(slc.get("min_atr_multiple", 0.8))
        max_k = float(slc.get("max_atr_multiple", 1.3))

        if df is not None:
            try:
                atr_price = self._compute_atr_from_df(df, period=atr_period)  # ATR en unités de prix
            except Exception:
                atr_price = None
            if atr_price and atr_price > 0:
                sl_min = min_k * atr_price
                sl_max = max_k * atr_price
                if sl_dist_price < sl_min:
                    return {"ok": False, "reason": f"sl_too_tight_vs_atr_{sl_dist_price:.6f}<{sl_min:.6f}"}
                if sl_dist_price > sl_max:
                    return {"ok": False, "reason": f"sl_too_wide_vs_atr_{sl_dist_price:.6f}>{sl_max:.6f}"}

        # --- 6) Stops level broker: refuser si SL/TP sous distance mini ---
        min_stop_price_dist = stops_lvl_points * point  # en unités de prix
        if min_stop_price_dist > 0:
            if sl_dist_price < min_stop_price_dist:
                return {"ok": False, "reason": f"sl_below_broker_min_{sl_pips_val:.2f}p<{stops_level_pips:.2f}p"}
            if tp_dist_price < min_stop_price_dist:
                return {"ok": False, "reason": f"tp_below_broker_min_{tp_pips_val:.2f}p<{stops_level_pips:.2f}p"}

        # --- 7) Spread & RR effectif ---
        if spread_pips > max_spread_pips_cfg:
            return {"ok": False, "reason": f"spread_too_wide_{spread_pips:.2f}p"}

        rr = tp_dist_price / sl_dist_price if sl_dist_price > 0 else 0.0

        # RR effectif (soustraire le spread du gain potentiel)
        if level_mode == "price":
            effective_tp_dist = max(0.0, tp_dist_price - spread_pts * point)
            rr_effective = (effective_tp_dist / sl_dist_price) if sl_dist_price > 0 else 0.0
        else:
            effective_tp_pips = max(0.0, tp_pips_val - spread_pips)
            rr_effective = (effective_tp_pips / sl_pips_val) if sl_pips_val > 0 else 0.0

        if rr_effective < min_rr:
            return {"ok": False, "reason": f"rr_effective_below_min_{rr_effective:.2f}_<{min_rr:.2f}"}

        # --- 8) Sizing au risque ---
        risk_amount = equity * (risk_pct / 100.0)
        try:
            raw_volume = risk_amount / (sl_dist_price * contract)  # lots = $risk / (Δprix × contract)
        except ZeroDivisionError:
            return {"ok": False, "reason": "invalid_contract_or_sl_dist"}

        # Quantification & bornes
        volume = self._quantize_volume(raw_volume, vol_min, vol_max, vol_step)

        # OK
        return {
            "ok": True,
            "volume": volume,
            "rr": rr,
            "rr_effective": rr_effective,
            "risk_amount": risk_amount,
            "entry_price": entry,
            "sl_price": sl_price,
            "tp_price": tp_price,
            "sl_pips": sl_pips_val,
            "tp_pips": tp_pips_val,
            "spread_pips": spread_pips,
            "stops_level_pips": stops_level_pips,
            "notes": notes,
            "level_mode": level_mode,
        }

        
    def _compute_atr_from_df(self, df, period: int = 14) -> float:
        """
        ATR simple sur le DF annoté (mêmes unités que le prix).
        Utilise high/low/close ; ignore NaN de tête de série.
        """
        import numpy as np
        if len(df) < period + 2:
            return float("nan")
        high = df["high"].astype(float)
        low  = df["low"].astype(float)
        close= df["close"].astype(float)

        prev_close = close.shift(1)
        tr1 = high - low
        tr2 = (high - prev_close).abs()
        tr3 = (low - prev_close).abs()
        tr = np.maximum(tr1, np.maximum(tr2, tr3))
        atr = tr.rolling(window=period, min_periods=period).mean().iloc[-1]
        try:
            return float(atr)
        except Exception:
            return float("nan")


