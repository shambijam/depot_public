# core/decision_pipeline.py
import logging
import json
import pandas as pd
import numpy as np
import math
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

    def _demo_unleash_override(
        self, asset_symbol: str, context: dict | None = None
    ) -> tuple[bool | None, str]:
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
            allow_unleash = bool(
                self.config_manager.get("debug.unleash.allow_all_entries_demo", False)
            )
            if not allow_unleash:
                return (None, "")
            # 2) Mode: uniquement en DEMO
            mode = str(self.config_manager.get("mode_execution", "DEMO")).upper()
            if mode != "DEMO":
                return (None, "")

            # 3) Garde-fous optionnels pour éviter des situations absurdes même en unleash
            #    a) Spread max
            try:
                max_spread_pts = self.config_manager.get(
                    "debug.unleash.max_spread_points", None
                )
            except Exception:
                max_spread_pts = None
            if max_spread_pts is not None:
                try:
                    last_spread = float(
                        context.get("market_data", {}).get(
                            "spread_points", float("inf")
                        )
                    )
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
                    open_pos_count = int(
                        context.get("account_state", {}).get("open_positions_count", 0)
                    )
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
            print(
                f"🤖 [DECISION] Base de connaissances: {len(config_knowledge_base)} stratégies disponibles"
            )

            optimal_config = self.select_optimal_config(
                analyzed_context, config_knowledge_base
            )
            if not optimal_config:
                self.logger.warning(
                    "Aucune stratégie optimale sélectionnée pour ce cycle. Pipeline arrêté."
                )
                print(f"🤖 [DECISION] ❌ Aucune stratégie optimale trouvée")
                return {
                    "timestamp_utc": datetime.now(UTC).isoformat(),
                    "context": analyzed_context,
                    "config_used": self.config_manager.get_current_dynamic_config(),
                    "final_decision": {},
                    "execution_context": {},
                }

            print(
                f"🤖 [DECISION] ✅ Stratégie optimale: {optimal_config.get('strategy_name', 'Unknown')}"
            )

            # 4) Adaptation de la configuration pour le cycle actuel
            print(f"🤖 [DECISION] Étape 4: Adaptation de configuration...")
            base_cfg = self.config_manager.get_current_dynamic_config()
            config_for_this_cycle = self.config_manager._merge_dicts(
                base_cfg, optimal_config
            )
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
                        sp = self.mt5_connector.get_spread_pips(
                            self.config_manager.get("asset_symbol_mapping", {}).get(
                                a, a
                            )
                        )
                    except Exception:
                        sp = float("inf")
                    spreads_pips[a] = (
                        float(sp) if isinstance(sp, (int, float)) else float("inf")
                    )

            # snapshots Katana (si PhaseObserver expose la méthode)
            if hasattr(self, "phase_observer") and hasattr(
                self.phase_observer, "get_katana_snapshot"
            ):
                for a in tradeables:
                    try:
                        snap = (
                            self.phase_observer.get_katana_snapshot(a, adapted_config)
                            or {}
                        )
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
            analyzed_context["execution_context"] = (
                execution_context  # pour consommation ultérieure (mecano/audit)
            )

            # 5) Décision de trade finale
            print(f"🤖 [DECISION] Étape 5: Décision de trade finale...")
            signals = analyzed_context.get("trading_signals", {}) or {}
            print(f"🤖 [DECISION] Signaux disponibles: {list(signals.keys())}")
            trade_decision = self.decide_trade_to_execute(
                analyzed_context,
                adapted_config,
                signals,
            )
            print(
                f"🤖 [DECISION] Décision finale: {trade_decision.get('action', 'AUCUNE')}"
            )

            # 5bis) RR projeté simple si overrides pips présents (utile pour audit)
            tp_pips = trade_decision.get("target_tp_pips")
            sl_pips = trade_decision.get("target_sl_pips")
            rr_projected = None
            try:
                if (
                    isinstance(tp_pips, (int, float))
                    and isinstance(sl_pips, (int, float))
                    and sl_pips > 0
                ):
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
            self.logger.error(
                f"Erreur critique dans institutional_decision_pipeline: {e}",
                exc_info=True,
            )
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

    def calculate_risk_parameters(
        self, context: dict, current_config: dict, trade_decision: dict
    ) -> dict:
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
        asset = str(trade_decision.get("asset", "")).upper()

        if action not in {"BUY", "SELL"} or not asset:
            return {"ok": False, "reason": "invalid_action_or_asset"}

        md = (context.get("market_data") or {}).get(asset, {}) or {}
        symbol_info = (
            md.get("symbol_info", {}) or {}
        )  # dict (MT5 SymbolInfo -> _asdict())
        account_info = context.get("account_info", {}) or {}

        # entry/sl/tp: idéalement fournis par la décision; sinon entry=prix courant
        entry = trade_decision.get("entry_price", md.get("current_price"))
        sl = trade_decision.get("sl_price")
        tp = trade_decision.get("tp_price")

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
        contract = float(symbol_info.get("trade_contract_size", 100000.0)) or 100000.0
        point = float(symbol_info.get("point", 0.00001)) or 0.00001
        digits = int(symbol_info.get("digits", 5))
        vol_min = float(symbol_info.get("volume_min", 0.01)) or 0.01
        vol_max = float(symbol_info.get("volume_max", 100.0)) or 100.0
        vol_step = float(symbol_info.get("volume_step", 0.01)) or 0.01
        spread_pts = float(md.get("current_spread_points", 0.0)) or 0.0

        # --- 3) Paramètres de risque (config) ---
        rm_cfg = (current_config or {}).get("risk_management", {}) or {}
        risk_pct = float(rm_cfg.get("risk_per_trade_pct", 0.5))  # % de l'equity
        min_rr = float(rm_cfg.get("min_rr", 1.2))
        max_spread_pips = float(
            rm_cfg.get("max_spread_pips", 2.0)
        )  # garde simple (scalping)
        fixed_volume_lots = rm_cfg.get("fixed_volume_lots")  # fallback si pas de SL/TP

        equity = float(
            account_info.get("equity", account_info.get("balance", 0.0)) or 0.0
        )
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
                "volume": self._quantize_volume(
                    fixed_volume_lots, vol_min, vol_max, vol_step
                ),
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
            rr_fmt = f"{rr:.2f}"
            min_rr_fmt = f"{min_rr:.2f}"
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

        ⚔️ Version STRICT 'katana midline scalp' (NO FALLBACK):
        - Gate d’entrée Bollinger médiane obligatoire (BUY ∈ [lower, mid], SELL ∈ [mid, upper])
        - 'entry_gate_ok' requis (depuis micro-phase Bollinger) + distance mini à la médiane
        - Refus explicite si données Bollinger ou prix invalides
        - Interdit en expansion/surge (anti-chaos) et hors range si requis
        - TPSL serrés basés sur half-band & médiane, rejet si RR < min_rr (aucun ajustement soft)
        - Attache systématique des niveaux Bollinger au package décisionnel

        ➕ Intégration optionnelle EMA/RSI/ATR Trailing (entrées uniquement)
        - Si activé et qu’une entrée BUY/SELL est proposée, on construit SL/TP (TP via min_rr*SL)
            et on bypass le gate Bollinger. La gestion des EXIT/UPDATE_TRAIL reste au position manager.
        """
        # 👉 Imports locaux nécessaires (évite NameError sur math/np/pd)

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

        # 1) Filtres pré-décision critiques (sécurité globale)
        if current_config.get(
            "halt_on_major_news", True
        ) and self.config_manager.check_news_schedule(
            context, context.get("economic_calendar", [])
        ):
            self.logger.warning(
                "Trade suspendu en raison d'un événement d'actualité majeur."
            )
            self.config_manager.log_decision(
                current_config, {}, context, "Trade bloqué: Actualité majeure."
            )
            return {}

        # 2) Récupérer le nom de stratégie
        strategy_name = current_config.get("strategy_name", "unknown")
        self.logger.info(
            f"🎯 CORE prend la décision avec paramètres de stratégie: {strategy_name}"
        )

        # 3) CORE évalue directement les signaux (sans délégation)
        trade_decision = self._core_evaluate_signals(
            context, current_config, signals, strategy_name
        )
        if not trade_decision:
            self.logger.info(
                f"CORE n'a trouvé aucune opportunité d'entrée ce cycle avec les paramètres '{strategy_name}'."
            )
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
            self.config_manager.log_decision(
                current_config, {}, context, "Décision ignorée (asset vide)."
            )
            return {}

        allowed_assets = set(map(str.upper, current_config.get("tradeable_assets", [])))
        if allowed_assets and asset_raw not in allowed_assets:
            self.logger.warning(
                f"Asset '{asset_raw}' non autorisé pour la stratégie '{strategy_name}'. Whitelist: {sorted(allowed_assets)}"
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
            self.logger.warning(
                f"🚫 Trade bloqué: limite journalière {max_trades_total} atteinte."
            )
            return {}
        if trades_for_asset_today >= max_trades_asset:
            self.logger.warning(
                f"🚫 Trade bloqué: limite {max_trades_asset} atteinte pour {asset_raw}."
            )
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

            # ⚠️ Correction anti-ambiguïté pandas (évite "truth value of a DataFrame is ambiguous")
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
                # On ne traite ici que les entrées BUY/SELL (uppercase)
                act = str((ema_dec or {}).get("action", "")).upper()
                if act in {"BUY", "SELL"}:
                    # fabrique TP via min_rr * distance_SL pour satisfaire calculate_risk_parameters
                    try:
                        sl_price = float(ema_dec["stop_loss"])
                    except Exception:
                        sl_price = float("nan")

                    if math.isfinite(sl_price):
                        min_rr = float(
                            (
                                (current_config.get("risk_management", {}) or {}).get(
                                    "min_rr", 1.5
                                )
                            )
                            or 1.5
                        )
                        sl_dist = abs(price - sl_price)
                        tp_price = (
                            price + min_rr * sl_dist
                            if act == "BUY"
                            else price - min_rr * sl_dist
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
                    else:
                        self.logger.info(
                            "EMA/RSI/ATR: SL invalide -> on ignore l'entrée EMA et on continue."
                        )
                elif act in {"EXIT_LONG", "EXIT_SHORT", "UPDATE_TRAIL"}:
                    # Gestion de position -> pas ici
                    self.logger.debug(
                        "EMA/RSI/ATR: action de gestion de position détectée (ignorée dans le decision engine)."
                    )

                    # ==========================================================
            # ➕ Gate "Big Reversal Candle" (OB/FVG/BOS confluence)
            # ==========================================================
            if not used_ema_decision:
                try:
                    md = (context.get("market_data", {}) or {}).get(asset_raw, {}) or {}
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
                except Exception as e:
                    self.logger.debug(f"Erreur gate Big Reversal Candle: {e}")

        # ==========================================================
        # 3bis) ⚔️ Gate STRICT 'Katana Midline Scalp' (NO FALLBACK)
        #       exécuté uniquement si on n'a PAS utilisé l'alternative EMA
        # ==========================================================
        if not used_ema_decision:
            scalp_cfg = (current_config.get("scalping") or {}).get(
                "boll_midline", {}
            ) or {}
            rr_min = float(scalp_cfg.get("min_rr", 1.1) or 1.1)
            k_halfband_tp = float(scalp_cfg.get("tp_halfband_k", 0.6) or 0.6)
            buffer_pips_min = float(scalp_cfg.get("buffer_pips_min", 1.5) or 1.5)
            require_range = bool(scalp_cfg.get("require_range_regime", True))
            block_on_expansion = bool(scalp_cfg.get("block_on_expansion", True))
            min_mid_ratio = float(
                scalp_cfg.get("min_mid_distance_ratio", 0.12) or 0.12
            )  # distance mini à la médiane

            def _get(path, default=None):
                try:
                    return path()  # lambda
                except Exception:
                    return default

            def _to_bool(x, default=False) -> bool:
                try:
                    if isinstance(x, (int, float)):
                        return bool(x)
                    if isinstance(x, str):
                        return x.strip().lower() in {"1", "true", "yes", "y", "on"}
                    return bool(x)
                except Exception:
                    return default

            boll = (
                signals.get("boll")
                or signals.get("bollinger")
                or signals.get("boll_micro")
                or signals.get("m1_boll")
                or {}
            )

            bb_mid = _num(_get(lambda: boll.get("bb_mid"), signals.get("bb_mid")))
            bb_up = _num(_get(lambda: boll.get("bb_upper"), signals.get("bb_upper")))
            bb_lo = _num(_get(lambda: boll.get("bb_lower"), signals.get("bb_lower")))
            is_range = _to_bool(
                _get(lambda: boll.get("is_range"), signals.get("is_range"))
            )
            is_exp = _to_bool(
                _get(lambda: boll.get("is_expansion"), signals.get("is_expansion"))
            )
            mid_entry = (
                str(
                    _get(lambda: boll.get("mid_entry"), signals.get("mid_entry", ""))
                    or ""
                )
            ).lower()

            # ✅ micro-phase strict flags
            entry_gate_ok = _to_bool(
                _get(lambda: boll.get("entry_gate_ok"), signals.get("entry_gate_ok")),
                default=False,
            )
            mid_distance_ratio = _num(
                _get(
                    lambda: boll.get("mid_distance_ratio"),
                    signals.get("mid_distance_ratio"),
                ),
                np.nan,
            )

            # ❌ NO FALLBACK: données Bollinger/price + gate micro-phase doivent être valides
            if any(
                not (isinstance(x, float) and math.isfinite(x))
                for x in (bb_mid, bb_up, bb_lo, price)
            ):
                self.logger.info(
                    "Rejet: données Bollinger/price invalides pour midline scalp (mode strict)."
                )
                return {}
            if not entry_gate_ok:
                self.logger.info(
                    "Rejet: entry_gate_ok=False depuis micro-phase (mode strict)."
                )
                return {}
            if isinstance(mid_distance_ratio, float) and math.isfinite(
                mid_distance_ratio
            ):
                if mid_distance_ratio < min_mid_ratio:
                    self.logger.info(
                        f"Rejet: distance à la médiane insuffisante ({mid_distance_ratio:.3f} < {min_mid_ratio:.3f})."
                    )
                    return {}

            # pip_size
            pip_size = None
            try:
                si = getattr(self, "symbol_info", None)
                point = 0.0
                if si is not None and hasattr(si, "point"):
                    point = float(getattr(si, "point") or 0.0)
                elif isinstance(si, dict):
                    point = float(si.get("point", 0.0) or 0.0)
                if point <= 0 and "point" in signals:
                    point = _num(signals.get("point"), 0.0)
                pip_size = point * 10.0 if point > 0 else None
            except Exception:
                pip_size = None

            half_band = (bb_up - bb_lo) / 2.0
            in_buy_zone = (price <= bb_mid) and (price >= bb_lo)
            in_sell_zone = (price >= bb_mid) and (price <= bb_up)

            if block_on_expansion and is_exp:
                self.logger.info("Rejet: expansion Bollinger active (anti-chaos).")
                return {}
            if require_range and not is_range:
                self.logger.info("Rejet: régime non-range pour midline scalp.")
                return {}

            if normalized_action == "BUY":
                if not (in_buy_zone and mid_entry == "buy"):
                    self.logger.info(
                        "Rejet BUY: condition midline non satisfaite (zone ou mid_entry)."
                    )
                    return {}
            elif normalized_action == "SELL":
                if not (in_sell_zone and mid_entry == "sell"):
                    self.logger.info(
                        "Rejet SELL: condition midline non satisfaite (zone ou mid_entry)."
                    )
                    return {}
            elif normalized_action == "CLOSE":
                pass  # fermeture autorisée

            # --- TPSL serrés (en pips) ---
            if normalized_action in {"BUY", "SELL"}:
                if not (pip_size and pip_size > 0):
                    self.logger.info("Rejet: pip_size indisponible (mode strict).")
                    return {}

                hb_pips = max(0.0, half_band / pip_size)
                target_tp_pips: float | None = None
                target_sl_pips: float | None = None

                if normalized_action == "BUY":
                    tp_to_mid_pips = max(0.0, (bb_mid - price) / pip_size)
                    fallback_tp = (
                        (k_halfband_tp * hb_pips) if hb_pips is not None else None
                    )
                    target_tp_pips = max(
                        tp_to_mid_pips, (fallback_tp or tp_to_mid_pips)
                    )
                    target_sl_pips = max(
                        buffer_pips_min, (price - bb_lo) / pip_size + buffer_pips_min
                    )
                else:  # SELL
                    tp_to_mid_pips = max(0.0, (price - bb_mid) / pip_size)
                    fallback_tp = (
                        (k_halfband_tp * hb_pips) if hb_pips is not None else None
                    )
                    target_tp_pips = max(
                        tp_to_mid_pips, (fallback_tp or tp_to_mid_pips)
                    )
                    target_sl_pips = max(
                        buffer_pips_min, (bb_up - price) / pip_size + buffer_pips_min
                    )

                # ❌ NO FALLBACK: RR doit respecter min_rr
                if not (target_tp_pips and target_sl_pips and target_sl_pips > 0):
                    self.logger.info("Rejet: TPSL non calculables (mode strict).")
                    return {}
                rr_est = float(target_tp_pips / target_sl_pips)
                if rr_est < rr_min:
                    self.logger.info(
                        f"Rejet: RR estimé {rr_est:.2f} < min_rr {rr_min:.2f} (mode strict)."
                    )
                    return {}

                # Injecter pour RiskEngine/Executor
                trade_decision["target_tp_pips"] = float(round(target_tp_pips, 3))
                trade_decision["target_sl_pips"] = float(round(target_sl_pips, 3))
                trade_decision["rule_name"] = "katana_midline_scalp_strict"
                trade_decision["level_mode"] = "boll_midline_strict"
                trade_decision["boll"] = {
                    "bb_mid": bb_mid,
                    "bb_upper": bb_up,
                    "bb_lower": bb_lo,
                }

        # 4) Contrôles compte/risque simples côté pipeline (pas d'exception)
        active_broker_account = context.get("active_broker_account", {})
        max_positions_for_account = active_broker_account.get("trade_settings", {}).get(
            "max_open_positions", 999
        )
        current_open_positions = context.get("open_positions", [])

        self.logger.debug(
            f"Positions ouvertes actuelles: {len(current_open_positions)} / Max: {max_positions_for_account}"
        )
        if len(current_open_positions) >= max_positions_for_account:
            self.logger.warning(
                f"Trade bloqué: Max positions ({max_positions_for_account}) atteint pour le compte {active_broker_account.get('account_id')}."
            )
            return {}

        # 5) Sizing au risque — instrumenté DIAG
        risk_params = self.calculate_risk_parameters(
            context, current_config, trade_decision
        )
        self.logger.debug(f"Paramètres de risque calculés: {risk_params}")

        if not risk_params or not bool(risk_params.get("ok", False)):
            reason = (risk_params or {}).get("reason", "risk_calc_failed")
            extras = {
                k: risk_params.get(k)
                for k in (
                    "sl_pips",
                    "tp_pips",
                    "spread_pips",
                    "rr_effective",
                    "stops_level_pips",
                    "level_mode",
                )
                if isinstance(risk_params, dict) and k in risk_params
            }
            _diag_size(asset_raw, reason, extras)
            self.logger.warning(f"Calcul de risque refusé pour {asset_raw}: {reason}")
            return {}

        if not (risk_params.get("volume", 0.0) > 0):
            _diag_size(
                asset_raw,
                "sizing_volume_zero_or_missing",
                {"ok": True, "volume": risk_params.get("volume")},
            )
            self.logger.warning(
                "Calcul de risque valide mais volume nul/invalide. Trade annulé."
            )
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
        Évalue les signaux et choisit l'actif à trader avec une logique permissive.
        - Pas de paramètres bloquants : tout est converti en scoring "soft".
        - Seul filtre dur conservé : confiance minimale (faible par défaut).
        - Les critères (BOS/MSS, OB, FVG, break M1, MTF, spread, etc.) influencent le score sans bloquer.
        ➕ Biais 'Katana Midline Scalp' :
        * Bonus si (is_range==True) & (is_expansion==False) & mid_entry ∈ {buy,sell} avec mid_entry_score élevé
        * Pénalité si expansion (chaos) ou bandes mal exploitées (touch répété sans revert)
        * Passe les méta-infos Bollinger au package décisionnel pour l’étape suivante
        """
        self.logger.info(
            f"🔍 CORE analyse {len(signals)} assets | strategy={strategy_name}"
        )

        strat = str(strategy_name or "").lower()
        is_scalping = "scalping" in strat

        # --- Seuils généraux (non stricts) ---
        min_confidence = float(config.get("min_confidence", 0.30))

        # Distances FVG / OB (proximité "soft")
        fvg_max = float(
            self.config_manager.get("entry_rules.scalping.fvg_max_distance_pips", 2.0)
            or 2.0
        )
        ob_max = float(
            self.config_manager.get("entry_rules.scalping.ob_max_distance_pips", 2.0)
            or 2.0
        )
        soft_mult = float(
            self.config_manager.get(
                "entry_rules.scalping.soft_distance_multiplier", 1.25
            )
            or 1.25
        )
        fvg_soft = fvg_max * soft_mult
        ob_soft = ob_max * soft_mult

        # Paramètres qualité (toujours SOFT)
        max_spread_pts = float(
            self.config_manager.get("entry_rules.scalping.max_spread_points", 50) or 50
        )

        # Pondérations (soft scoring)
        W_CONF = 1.00
        W_BOS = 0.15
        W_OB = 0.10
        W_FVG = 0.08
        W_M1_BREAK = 0.12
        W_MTF_HIT = 0.10
        PEN_SPREAD = -0.10
        PEN_LOW_VOL = -0.20
        BASE_BIAS = float(
            (config.get("decision_engine") or {})
            .get("scoring", {})
            .get("strategy_bias", 0.30)
            or 0.30
        )

        # ➕ Pondérations 'Katana Midline'
        W_BOLL_MID_OK = 0.18  # is_range & !expansion & mid_entry présent
        W_MID_SCORE_K = 0.20  # contribution de mid_entry_score (0..1) * K
        W_MEANREV_K = 0.08  # bonus mean_revert (range)
        W_BREAK_PENALTY = (
            -0.06
        )  # petite pénalité breakout score en régime range (évite poursuites)
        PEN_EXPANSION = (
            -0.25
        )  # blocage soft si expansion (sera dur dans le gate suivant)
        PEN_TOUCH_ONLY = -0.05  # si touch bande sans signal exploitable

        best_asset, best_score, best_signals = None, float("-inf"), None

        for asset, s in (signals or {}).items():
            if not isinstance(s, dict) or not s:
                continue

           # --- Phase & confiance : priorité aux valeurs stabilisées par la mémoire ---
            phase = str(
                s.get("phase_memory_stabilized", s.get("phase", "no_clear_phase"))
            )
            confidence = float(
                s.get("confidence_stabilized", s.get("confidence_score", s.get("confidence", 0.0))) or 0.0
            )

            if confidence < min_confidence:
                self.logger.debug(
                    "Asset %s ignoré: confidence %.3f < %.3f (phase=%s)",
                    asset,
                    confidence,
                    min_confidence,
                    phase,
                )
                continue


            # Composantes de confluence (SOFT)
            bos_ok = bool(
                s.get("bos_mss_detected")
                or (s.get("bos_mss_details") or {}).get("confirmed")
                or (s.get("bos_mss_details") or {}).get("is_confirmed")
            )
            ob_det = bool(
                s.get("ob_detected")
                or s.get("order_block")
                or s.get("order_block_ml_enhanced")
            )
            fvg_det = bool(s.get("fvg_detected") or s.get("fvg_enhanced"))

            fvg_dist = float(
                (s.get("fvg_details") or {}).get(
                    "distance_pips", s.get("fvg_distance_pips", 1e9)
                )
                or 1e9
            )
            ob_dist = float(
                (s.get("ob_details") or {}).get(
                    "distance_pips", s.get("ob_distance_pips", 1e9)
                )
                or 1e9
            )
            fvg_close_soft = fvg_det and (fvg_dist <= fvg_soft)
            ob_close_soft = (ob_dist <= ob_soft) and (ob_det or ob_dist <= ob_max)

            # Break M1 aligné MTF (SOFT)
            mtf_direction = str(s.get("mtf_direction", "none")).lower()
            m1_hh_break = bool(s.get("m1_last_hh_break", False))
            m1_ll_break = bool(s.get("m1_last_ll_break", False))
            if mtf_direction == "up":
                m1_break = m1_hh_break
            elif mtf_direction == "down":
                m1_break = m1_ll_break
            else:
                m1_break = bool(
                    s.get("m1_break", False) or s.get("bos_mss_enhanced", False)
                )

            # MTF hits (SOFT)
            mtf_hits = 0
            for k in ("mtf_hits", "mtf_agreements", "mtf_confluence"):
                try:
                    mtf_hits = max(mtf_hits, int(s.get(k, 0)))
                except Exception:
                    pass
            for k in ("m1_align", "m5_align", "m15_align"):
                if bool(s.get(k, False)):
                    mtf_hits += 1

            # Qualité marché (pénalités soft)
            try:
                spread_points = float(
                    s.get("current_spread_points", s.get("spread", float("inf")))
                    or float("inf")
                )
            except Exception:
                spread_points = float("inf")
            try:
                vol_z = float(s.get("volume_zscore", 0.0) or 0.0)
            except Exception:
                vol_z = 0.0

            # --------- BOLLINGER midline (multi-sources tolérantes) ---------
            boll = (
                s.get("boll")
                or s.get("bollinger")
                or s.get("boll_micro")
                or (s.get("signals") or {}).get("micro_phase_hint")
                or {}
            )

            is_range = bool(boll.get("is_range", False))
            is_expansion = bool(boll.get("is_expansion", False))
            band_touch = boll.get("band_touch")  # 'upper'/'lower'/None
            mid_entry = (str(boll.get("mid_entry", "")) or "").lower()
            mid_score = float(boll.get("mid_entry_score", 0.0) or 0.0)
            mr_score = float(boll.get("mean_revert_score", 0.0) or 0.0)
            br_score = float(boll.get("breakout_score", 0.0) or 0.0)

            # --- Scoring permissif global ---
            score = 0.0
            score += W_CONF * confidence
            score += BASE_BIAS
            if bos_ok:
                score += W_BOS
            if ob_close_soft:
                score += W_OB
            if fvg_close_soft:
                score += W_FVG
            if m1_break:
                score += W_M1_BREAK
            score += mtf_hits * W_MTF_HIT

            if spread_points > max_spread_pts:
                score += PEN_SPREAD
            if vol_z < 0.0:
                score += PEN_LOW_VOL

            # --- Biais Katana Midline (SOFT dans le score; le vrai gate est plus loin) ---
            if is_scalping:
                if is_expansion:
                    score += PEN_EXPANSION  # chaos
                if is_range and not is_expansion:
                    # mid_entry présent → bonus
                    if mid_entry in ("buy", "sell"):
                        score += W_BOLL_MID_OK
                    # contribution continue du mid_entry_score (plus il est haut, mieux c'est)
                    score += mid_score * W_MID_SCORE_K
                    # Encourager mean-revert en range, décourager breakout chasing
                    score += mr_score * W_MEANREV_K
                    score += br_score * W_BREAK_PENALTY
                # petite pénalité si on touche une bande sans vraie structure
                if (
                    band_touch in ("upper", "lower")
                    and mid_score < 0.4
                    and mr_score < 0.5
                    and br_score < 0.5
                ):
                    score += PEN_TOUCH_ONLY

            self.logger.debug(
                "CORE score %s -> %.4f | conf=%.3f bos=%s ob_soft=%s fvg_soft=%s m1_break=%s mtf=%d "
                "spread=%.1f volZ=%.2f | boll: range=%s exp=%s mid=%s(%.2f) mr=%.2f br=%.2f",
                asset,
                score,
                confidence,
                bos_ok,
                ob_close_soft,
                fvg_close_soft,
                m1_break,
                mtf_hits,
                spread_points,
                vol_z,
                is_range,
                is_expansion,
                mid_entry,
                mid_score,
                mr_score,
                br_score,
            )

            if score > best_score:
                best_asset, best_score, best_signals = asset, score, s
                # s contient peut-être déjà 'boll' → on s’assure d’unifier la clé
                if isinstance(boll, dict) and boll:
                    best_signals.setdefault("boll", boll)
                    # expose aussi quelques alias plats (exploités par d’autres briques)
                    best_signals.setdefault("bb_mid", boll.get("bb_mid"))
                    best_signals.setdefault("bb_upper", boll.get("bb_upper"))
                    best_signals.setdefault("bb_lower", boll.get("bb_lower"))
                    best_signals.setdefault("mid_entry", mid_entry)
                    best_signals.setdefault("mid_entry_score", mid_score)
                    best_signals.setdefault("boll_mean_revert_score", mr_score)
                    best_signals.setdefault("boll_breakout_score", br_score)
                    best_signals.setdefault("boll_signal", boll.get("signal"))

        if not best_asset:
            self.logger.info(
                "CORE: aucun actif au-dessus du seuil de confiance minimal."
            )
            return {}

        self.logger.info(f"🎯 CORE sélectionne: {best_asset} (score: {best_score:.3f})")
        return self._core_build_trade_decision(
            best_asset, best_signals, config, context
        )

    def _core_build_trade_decision(
        self,
        asset: str,
        signals: Dict[str, Any],
        config: Dict[str, Any],
        context: Dict[str, Any],
    ) -> Dict[str, Any]:
        """
        Construit la décision finale de trade à partir des signaux, en mode permissif.
        - Direction priorisée par MTF, sinon par phase, sinon par Bollinger mid_entry (scalp).
        - Injecte des *hints* TP/SL serrés “midline” quand le setup Bollinger le justifie.
        - Aucune contrainte dure ici : les gardes stricts (expansion/ATR/stops) sont vérifiés plus tard.
        """
        from datetime import datetime, timezone

        # ---- DIAG (informative, non bloquant) ----
        try:
            from core.diagnostics import get_tracker_from_context

            def _diag(reason: str, extra: dict | None = None):
                try:
                    get_tracker_from_context(context).note(
                        asset, "core", reason, extra or {}
                    )
                except Exception:
                    pass

            def _diag_selected(rule: str, conf: float | None):
                try:
                    get_tracker_from_context(context).set_selected(asset, rule, conf)
                except Exception:
                    pass

        except Exception:

            def _diag(*a, **k):
                pass

            def _diag_selected(*a, **k):
                pass

       # Phase stabilisée par mémoire prioritaire
        phase = str(
            signals.get("phase_memory_stabilized", signals.get("phase", "no_clear_phase"))
        ).lower()
        # Prix (tolérant multi-sources)
        current_price = None
        for k in ("current_price", "last_close", "close", "entry_price"):
            if k in signals and isinstance(signals[k], (int, float)):
                current_price = float(signals[k])
                break
        if not isinstance(current_price, (int, float)) or current_price <= 0:
            self.logger.error("Prix actuel manquant ou invalide pour %s", asset)
            _diag("invalid_price_or_close", {"close": signals.get("close")})
            return {}

        strategy_name = str(config.get("strategy_name", "")).lower() or "scalping"

        # ---------- Symboles / conversions ----------
        # point / digits : on tente via signaux puis config manager
        point = float(signals.get("symbol_point_value") or 0.0)
        if point <= 0:
            point = float(
                self.config_manager.get("risk_management_settings.default_point", 1e-5)
                or 1e-5
            )
        digits = int(signals.get("symbol_digits") or (5 if point <= 1e-5 else 3))
        points_per_pip = 10.0 if digits in (3, 5) else 1.0
        pip_size = point * points_per_pip

        # Spread courant (points -> pips)
        try:
            spread_points = float(
                signals.get("current_spread_points", signals.get("spread", 0.0)) or 0.0
            )
        except Exception:
            spread_points = 0.0
        spread_pips = max(0.0, spread_points / (points_per_pip or 1.0))

        # ---------- Direction (MTF > phase > mid_entry) ----------
        action: Optional[str] = None
        mtf_dir = str(signals.get("mtf_direction", "none")).lower()
        if strategy_name == "scalping" and mtf_dir in ("up", "down"):
            action = "BUY" if mtf_dir == "up" else "SELL"

        if action is None:
            if any(
                k in phase for k in ["bull", "up", "accumulation", "expansion", "trend"]
            ):
                action = "BUY"
            elif any(k in phase for k in ["bear", "down", "distribution"]):
                action = "SELL"

        # Bollinger (pour éventuellement départager / midline hints)
        boll = (
            signals.get("boll")
            or signals.get("bollinger")
            or signals.get("boll_micro")
            or (signals.get("signals") or {}).get("micro_phase_hint")
            or {}
        )
        bb_mid = boll.get("bb_mid")
        bb_upper = boll.get("bb_upper")
        bb_lower = boll.get("bb_lower")
        is_range = bool(boll.get("is_range", False))
        is_exp = bool(boll.get("is_expansion", False))
        mid_entry = (str(boll.get("mid_entry", "")) or "").lower()
        mid_entry_score = float(boll.get("mid_entry_score", 0.0) or 0.0)

        # Si toujours pas d'action, tente la midline
        if action is None and is_range and not is_exp and mid_entry in ("buy", "sell"):
            action = "BUY" if mid_entry == "buy" else "SELL"
            _diag(
                "action_from_boll_mid_entry",
                {"mid_entry": mid_entry, "score": mid_entry_score},
            )

        if action is None:
            self.logger.info(
                "%s: pas de direction claire (phase=%s, mtf=%s).",
                asset,
                phase or "empty",
                mtf_dir,
            )
            _diag("no_direction", {"phase": phase, "mtf": mtf_dir})
            return {}
        action = action.upper()

        # ---------- Métriques utiles (informatives) ----------
        atr_m1 = float(signals.get("atr_m1", 0.0) or 0.0)
        atr_m5 = float(signals.get("atr_m5", 0.0) or 0.0)
        atr_m1_pips = (atr_m1 / pip_size) if pip_size > 0 else 0.0
        atr_m5_pips = (atr_m5 / pip_size) if pip_size > 0 else 0.0

        atr_min_soft = float(
            self.config_manager.get("entry_rules.scalping.min_atr_m1_pips", 0.0) or 0.0
        )
        max_spread_pips_soft = float(
            self.config_manager.get(
                "entry_rules.scalping.max_spread_pips",
                (
                    self.config_manager.get(
                        "entry_rules.scalping.max_spread_points", 50
                    )
                    or 50
                )
                / (points_per_pip or 1.0),
            )
            or 9999
        )
        if atr_min_soft > 0 and atr_m1_pips < atr_min_soft:
            self.logger.info(
                "%s: ATR M1 faible (%.2f < %.2f) — informatif.",
                asset,
                atr_m1_pips,
                atr_min_soft,
            )
            _diag("soft_atr_m1_low", {"atr_m1_pips": atr_m1_pips, "min": atr_min_soft})
        if spread_pips > max_spread_pips_soft:
            self.logger.info(
                "%s: spread %.2fp > %.2fp — informatif.",
                asset,
                spread_pips,
                max_spread_pips_soft,
            )
            _diag(
                "soft_spread_high",
                {"spread_pips": spread_pips, "max_soft": max_spread_pips_soft},
            )

        # ---------- SL/TP dynamiques de base (fallback) ----------
        base_sl_pips = float(config.get("stop_loss_pips", 20) or 20)
        base_tp_pips = float(config.get("take_profit_pips", 40) or 40)

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
        sl_low = float(scalping_adapt.get("stop_loss_pips_low_vol", base_sl_pips))
        tp_low = float(scalping_adapt.get("take_profit_pips_low_vol", base_tp_pips))

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

        # Ajustements spread pour préserver le R:R effectif
        tp_pips = max(1.0, tp_pips - spread_pips)
        sl_pips = max(1.0, sl_pips + spread_pips * 0.5)

        # Cap SL micro (si configuré) : on tronque, jamais de rejet ici
        sl_cap = float(
            self.config_manager.get("entry_rules.scalping.max_stop_pips_scalp", 0.0)
            or 0.0
        )
        if sl_cap > 0 and sl_pips > sl_cap:
            _diag("sl_capped", {"from": sl_pips, "to": sl_cap})
            sl_pips = sl_cap

        # ---------- ➕ Hints “Katana midline” (si setup valide) ----------
        # Objectif : Buy < mid → TP vers mid (overshoot léger), SL au-delà de la bande opposée (+buffer)
        #            Sell > mid → TP vers mid, SL au-delà de l’autre bande (+buffer)
        rule_name = "core_phase_permissive"
        level_mode = None
        tp_hint = None
        sl_hint = None

        try:
            mid_mode_cfg = (
                (config.get("entry_rules") or {}).get("scalping") or {}
            ).get("boll_midline", {}) or {}
            rr_min = float(mid_mode_cfg.get("min_rr", 1.1) or 1.1)
            k_halfband_tp = float(mid_mode_cfg.get("tp_halfband_k", 0.6) or 0.6)
            buffer_pips_min = float(mid_mode_cfg.get("buffer_pips_min", 1.5) or 1.5)

            valid_boll = all(
                isinstance(v, (int, float)) for v in (bb_mid, bb_upper, bb_lower)
            )
            if (
                valid_boll
                and is_range
                and not is_exp
                and mid_entry in ("buy", "sell")
                and mid_entry_score >= 0.55
            ):
                half_band_price = (bb_upper - bb_lower) / 2.0
                half_band_pips = (half_band_price / pip_size) if pip_size > 0 else None
                level_mode = "boll_midline"
                rule_name = "core_katana_midline"

                if action == "BUY":
                    # Entrée idéale : sous la médiane
                    if isinstance(half_band_pips, float):
                        to_mid_pips = max(0.0, (bb_mid - current_price) / pip_size)
                        tp_hint = max(to_mid_pips, k_halfband_tp * half_band_pips)
                        sl_hint = max(
                            buffer_pips_min,
                            (current_price - bb_lower) / pip_size + buffer_pips_min,
                        )
                else:  # SELL
                    if isinstance(half_band_pips, float):
                        to_mid_pips = max(0.0, (current_price - bb_mid) / pip_size)
                        tp_hint = max(to_mid_pips, k_halfband_tp * half_band_pips)
                        sl_hint = max(
                            buffer_pips_min,
                            (bb_upper - current_price) / pip_size + buffer_pips_min,
                        )

                # RR soft min
                if (
                    isinstance(tp_hint, float)
                    and isinstance(sl_hint, float)
                    and sl_hint > 0
                    and (tp_hint / sl_hint) < rr_min
                ):
                    tp_hint = rr_min * sl_hint

                # Si les hints existent, on préfère ces cibles serrées
                if isinstance(tp_hint, float) and tp_hint > 0:
                    tp_pips = tp_hint
                if isinstance(sl_hint, float) and sl_hint > 0:
                    sl_pips = sl_hint

                _diag(
                    "midline_hints_set",
                    {
                        "tp_pips": tp_pips,
                        "sl_pips": sl_pips,
                        "rr_soft": (tp_pips / max(sl_pips, 1e-12)),
                    },
                )
        except Exception as e:
            self.logger.debug(f"[core_build] midline hints: {e}")

        # ---------- Trace de décision ----------
        decision_trace = {
            "phase": phase,
            "phase_m1": signals.get("phase_m1"),
            "phase_m5": signals.get("phase_m5"),
            "phase_m15": signals.get("phase_m15"),
            "mtf_direction": mtf_dir,
            "m1_break_in_direction": bool(
                signals.get("m1_last_hh_break")
                if mtf_dir == "up"
                else signals.get("m1_last_ll_break")
            ),
            "m1_retest_confirmation": bool(signals.get("m1_retest_confirmation")),
            "bos_mss_details": signals.get("bos_mss_details") or {},
            "fvg_details": signals.get("fvg_details") or {},
            "ob_details": signals.get("ob_details") or {},
            "atr_m1_pips": atr_m1_pips,
            "atr_m5_pips": atr_m5_pips,
            "spread_pips": spread_pips,
            "volatility_pct": vol_pct,
            "regime": ("range" if is_range else "non_range"),
            "boll": {
                "bb_mid": bb_mid,
                "bb_upper": bb_upper,
                "bb_lower": bb_lower,
                "is_range": is_range,
                "is_expansion": is_exp,
                "mid_entry": mid_entry,
                "mid_entry_score": mid_entry_score,
                "mean_revert_score": boll.get("mean_revert_score"),
                "breakout_score": boll.get("breakout_score"),
                "signal": boll.get("signal"),
            },
            "level_mode": level_mode or "standard",
        }

        timestamp = (
            context.get("current_time_utc") or datetime.now(timezone.utc).isoformat()
        )
        trade_decision = {
            "action": action,
            "asset": asset,
            "strategy_type": f"core_{config.get('strategy_name', 'decision')}",
            "entry_price": current_price,
            "target_sl_pips": float(round(sl_pips, 3)),
            "target_tp_pips": float(round(tp_pips, 3)),
            "rule_name": rule_name,
            "confidence": float(
                signals.get("confidence_stabilized", signals.get("confidence_score", 0.0) or 0.0)
            ),
            "timestamp": timestamp,
            "magic_number": int(config.get("magic_number", 999_999)),
            "decision_trace": decision_trace,
            # Hints explicites pour l’executor/risk engine
            "level_mode": level_mode or "standard",
            "sl_pips_hint": (
                float(round(sl_hint, 3)) if isinstance(sl_hint, float) else None
            ),
            "tp_pips_hint": (
                float(round(tp_hint, 3)) if isinstance(tp_hint, float) else None
            ),
            # Expose Bollinger à l’executor (pour mode 'boll_midline' dans _calculate_sl_tp_prices)
            "boll": (
                {"bb_mid": bb_mid, "bb_upper": bb_upper, "bb_lower": bb_lower}
                if all(
                    isinstance(v, (int, float)) for v in (bb_mid, bb_upper, bb_lower)
                )
                else {}
            ),
        }

        self.logger.info(
            "CORE %s %s @ %.5f | SL=%.2fp, TP=%.2fp | spread=%.2fp, ATR_M5=%.2fp | rule=%s",
            action,
            asset,
            current_price,
            trade_decision["target_sl_pips"],
            trade_decision["target_tp_pips"],
            spread_pips,
            atr_m5_pips,
            rule_name,
        )
        _diag_selected(
            trade_decision.get("rule_name"), trade_decision.get("confidence")
        )

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

    def calculate_risk_parameters(
        self, context: dict, current_config: dict, trade_decision: dict
    ) -> dict:
        """
        Sizing au risque — version STRICT-AWARE (midline strict) & safe par défaut
        -------------------------------------------------------------------------
        Priorités des niveaux (de la plus forte à la plus faible):
        1) Hints midline (sl_pips_hint/tp_pips_hint) — si level_mode == "boll_midline" / "boll_midline_strict"
        2) target_sl_pips / target_tp_pips        — décision de base
        3) sl_price / tp_price                    — si fournis explicitement en prix
        4) fallback dynamique via default_sl_pips (config risk_management) si SL absent

        REFUS explicites (hard):
        - action/symbole/prix invalide
        - incohérence directionnelle (BUY: sl<entry<tp ; SELL: tp<entry<sl)
        - equity nulle
        - contraintes BROKER (stops_level) impossibles à satisfaire
        - (STRICT midline) spread au-delà du cap
        - (STRICT midline) RR_effectif < min_rr (pas d’étirement “TP pour atteindre RR”)
        - (STRICT midline) volume quantifié ≤ 0

        Ajustements permissifs (non stricts / legacy):
        - SL borné dans [min_k*ATR ; max_k*ATR] (si ATR dispo)
        - Spread noté ; on peut étirer TP pour RR_effectif ≥ min_rr (capé à max_tp_to_sl_ratio)
        - Rounding aux `digits` après ajustements
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

        # DataFrame ATR: priorité M1 (scalping)
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

        # --- 2) Broker/symbole (unités et contraintes) ---
        contract = float(symbol_info.get("trade_contract_size", 100000.0) or 100000.0)
        point = float(symbol_info.get("point", 0.00001) or 0.00001)
        digits = int(symbol_info.get("digits", 5) or 5)
        vol_min = float(symbol_info.get("volume_min", 0.01) or 0.01)
        vol_max = float(symbol_info.get("volume_max", 100.0) or 100.0)
        vol_step = float(symbol_info.get("volume_step", 0.01) or 0.01)

        # Stops level (robuste aux variations de clé)
        stops_lvl_points = (
            float(symbol_info.get("trade_stops_level", 0.0) or 0.0)
            if "trade_stops_level" in symbol_info
            else float(symbol_info.get("stops_level", 0.0) or 0.0)
        )

        # Spread actuel (points)
        spread_pts = float(md.get("current_spread_points", 0.0) or 0.0)

        # Pips (FX: digits 3/5 => 10 points = 1 pip)
        pip_points = 10.0 if digits in (3, 5) else 1.0
        pip_size = max(1e-12, point * pip_points)  # garde-fou
        spread_pips = spread_pts / pip_points
        stops_level_pips = stops_lvl_points / pip_points
        min_stop_price_dist = stops_lvl_points * point

        # --- 3) Risque (config) & adaptation ---
        rm_cfg = (current_config or {}).get("risk_management", {}) or {}
        risk_pct = float(rm_cfg.get("risk_per_trade_pct", 0.25))
        min_rr = float(rm_cfg.get("min_rr", 1.8))
        max_spread_pips_cfg = float(rm_cfg.get("max_spread_pips", 1.2))
        max_tp_sl_ratio = float(rm_cfg.get("max_tp_to_sl_ratio", 3.5))
        default_sl_pips = float(rm_cfg.get("default_sl_pips", 10.0))  # ✅ ajout

        # Mode strict pour midline ?
        level_mode_in = str(trade_decision.get("level_mode", "")).lower()
        strict_cfg = (current_config or {}).get("strict_modes", {}) or {}
        strict_for_midline = bool(
            strict_cfg.get("midline", True)
        )  # default True: strict sur midline
        is_midline_strict = level_mode_in in {"boll_midline_strict"} or (
            level_mode_in == "boll_midline" and strict_for_midline
        )

        # Adaptation high_vol (si meta/regime_tag fourni)
        meta = trade_decision.get("meta", {}) or {}
        regime_tag = str(meta.get("regime_tag", "")).lower()
        adapt_risk = (current_config or {}).get("adaptation_settings", {}).get(
            "risk_adjustment", {}
        ) or {}
        if regime_tag == "high_vol":
            mult = float(
                adapt_risk.get("risk_reduction_multiplier_high_vol", 1.0) or 1.0
            )
            min_after = float(
                adapt_risk.get("min_risk_percent_after_adjustment", 0.01) or 0.01
            )
            risk_pct = max(min_after, risk_pct * mult)

        # Cap global
        global_cap_pct = float(
            self.config_manager.get("global_safety.max_risk_per_trade_percent", 2.0)
            or 2.0
        )
        risk_pct = min(risk_pct, global_cap_pct)

        # Equity
        equity = float(
            account_info.get("equity", account_info.get("balance", 0.0)) or 0.0
        )
        if equity <= 0:
            return {"ok": False, "reason": "no_equity"}

        # --- 4) Niveaux: PRIX vs PIPS (priorités avec MIDLINE) ---
        sl_hint = trade_decision.get("sl_pips_hint")
        tp_hint = trade_decision.get("tp_pips_hint")

        # Target pips (décision standard)
        sl_pips_target = trade_decision.get("target_sl_pips")
        tp_pips_target = trade_decision.get("target_tp_pips")

        # Niveaux prix explicitement fournis
        sl_price_in = trade_decision.get("sl_price")
        tp_price_in = trade_decision.get("tp_price")

        sl_pips_val = None
        tp_pips_val = None

        if (
            level_mode_in in {"boll_midline", "boll_midline_strict"}
            and isinstance(sl_hint, (int, float))
            and isinstance(tp_hint, (int, float))
        ):
            sl_pips_val = float(sl_hint)
            tp_pips_val = float(tp_hint)
            notes.append("levels_from_midline_hints")
        elif isinstance(sl_pips_target, (int, float)) and isinstance(
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
        else:
            # ✅ Nouveau : fallback dynamique si SL absent
            sl_pips_val = default_sl_pips
            tp_pips_val = None
            notes.append(f"used_default_sl:{default_sl_pips}p")

        # Vérification distance SL
        if not (sl_pips_val and sl_pips_val > 0):
            return {"ok": False, "reason": "invalid_sl_distance"}

        # Reconstruire les PRIX depuis les pips
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

        # --- 5) Bornes via ATR (ajustements seulement si NON strict) ---
        atr_settings = rm_cfg.get("atr_settings") or {}
        slc = rm_cfg.get("sl_constraints", {}) or {}
        atr_period = int(atr_settings.get("period", 14))
        min_k = float(slc.get("min_atr_multiple", 0.8))
        max_k = float(slc.get("max_atr_multiple", 1.3))

        atr_price = None
        try:
            if df is not None:
                atr_price = self._compute_atr_from_df(df, period=atr_period)
        except Exception:
            atr_price = None

        if not (isinstance(atr_price, float) and atr_price > 0):
            atr_m1_pips = None
            dt = trade_decision.get("decision_trace") or {}
            if isinstance(meta.get("atr_m1_pips"), (int, float)):
                atr_m1_pips = float(meta["atr_m1_pips"])
            elif isinstance(dt.get("atr_m1_pips"), (int, float)):
                atr_m1_pips = float(dt["atr_m1_pips"])
            if isinstance(atr_m1_pips, float) and atr_m1_pips > 0:
                atr_price = atr_m1_pips * pip_size

        if (not is_midline_strict) and isinstance(atr_price, float) and atr_price > 0:
            sl_min = max(0.0, min_k * atr_price)
            sl_max = max(sl_min, max_k * atr_price)
            if sl_dist_price < sl_min:
                sl_dist_price = sl_min
                sl_pips_val = sl_dist_price / pip_size
                sl_price = (
                    (entry - sl_dist_price)
                    if action == "BUY"
                    else (entry + sl_dist_price)
                )
                notes.append(f"sl_adjusted_to_atr_min:{sl_pips_val:.2f}p")
            elif sl_dist_price > sl_max:
                sl_dist_price = sl_max
                sl_pips_val = sl_dist_price / pip_size
                sl_price = (
                    (entry - sl_dist_price)
                    if action == "BUY"
                    else (entry + sl_dist_price)
                )
                notes.append(f"sl_capped_to_atr_max:{sl_pips_val:.2f}p")

        # --- 6) Stops level broker (hard) ---
        if min_stop_price_dist > 0:
            if sl_dist_price < min_stop_price_dist:
                sl_dist_price = min_stop_price_dist
                sl_pips_val = sl_dist_price / pip_size
                sl_price = (
                    (entry - sl_dist_price)
                    if action == "BUY"
                    else (entry + sl_dist_price)
                )
                notes.append(f"sl_raised_to_broker_min:{sl_pips_val:.2f}p")
            if tp_dist_price and tp_dist_price < min_stop_price_dist:
                tp_dist_price = min_stop_price_dist
                tp_pips_val = tp_dist_price / pip_size
                tp_price = (
                    (entry + tp_dist_price)
                    if action == "BUY"
                    else (entry - tp_dist_price)
                )
                notes.append(f"tp_raised_to_broker_min:{tp_pips_val:.2f}p")

        # --- Rounding prix ---
        if isinstance(digits, int) and digits >= 0:
            sl_price = round(sl_price, digits)
            if tp_price:
                tp_price = round(tp_price, digits)

        # --- 7) Spread & RR ---
        rr = (
            (tp_dist_price / sl_dist_price)
            if (tp_dist_price and sl_dist_price > 0)
            else None
        )
        spread_comp_price = spread_pts * point
        effective_tp_dist = max(0.0, (tp_dist_price or 0.0) - spread_comp_price)
        rr_effective = (effective_tp_dist / sl_dist_price) if sl_dist_price > 0 else 0.0

        if is_midline_strict:
            if spread_pips > max_spread_pips_cfg:
                return {
                    "ok": False,
                    "reason": f"spread_too_high_{spread_pips:.2f}p>{max_spread_pips_cfg:.2f}p",
                }
            if rr is not None and rr_effective < min_rr:
                return {
                    "ok": False,
                    "reason": f"rr_effective_below_min_{rr_effective:.2f}<{min_rr:.2f}",
                }

        # --- 8) Sizing au risque ---
        risk_amount = equity * (risk_pct / 100.0)
        try:
            raw_volume = risk_amount / (sl_dist_price * contract)
        except ZeroDivisionError:
            return {"ok": False, "reason": "invalid_contract_or_sl_dist"}

        volume = self._quantize_volume(raw_volume, vol_min, vol_max, vol_step)

        if is_midline_strict and (not isinstance(volume, (int, float)) or volume <= 0):
            return {"ok": False, "reason": "volume_after_quantization_zero"}

        return {
            "ok": True,
            "volume": volume,
            "rr": rr,
            "rr_effective": rr_effective,
            "risk_amount": risk_amount,
            "entry_price": entry,
            "sl_price": sl_price,
            "tp_price": tp_price,
            "sl_pips": sl_dist_price / pip_size,
            "tp_pips": (tp_dist_price / pip_size) if tp_dist_price else None,
            "spread_pips": spread_pips,
            "stops_level_pips": stops_level_pips,
            "notes": notes,
            "level_mode": level_mode_in
            or ("boll_midline" if "levels_from_midline_hints" in notes else "pips"),
        }
