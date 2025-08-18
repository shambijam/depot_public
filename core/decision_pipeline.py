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

    def institutional_decision_pipeline(
        self, context: Dict[str, Any]
    ) -> Dict[str, Any]:
        """
        Orchestre le pipeline de décision de haut niveau pour un cycle de trading.
        Cette fonction exécute la séquence complète d'analyse et de décision en utilisant une
        configuration transitoire en mémoire pour une efficacité maximale.

        Args:
            context (Dict): Le contexte système et marché complet pour le cycle actuel.

        Returns:
            Dict: Un paquet de décision structuré pour l'audit et l'exécution.
        """
        self.logger.info("--- Démarrage du Pipeline de Décision Institutionnel ---")
        print(f"🤖 [DECISION] Début du pipeline institutionnel")

        try:
            # 1. Analyse et enrichissement du contexte
            print(f"🤖 [DECISION] Étape 1: Analyse du contexte...")
            analyzed_context = self.config_manager.analyze_context(context)
            print(f"🤖 [DECISION] Contexte analysé avec succès")

            # 2. Consultation facultative de l'IA
            print(f"🤖 [DECISION] Étape 2: Vérification IA...")
            if False:  # IA déplacée vers audit arrière-plan
                print(f"🤖 [DECISION] IA activée, sélection des assets...")
                opportunities = self.select_assets_to_trade(analyzed_context)
                print(f"🤖 [DECISION] Assets sélectionnés: {opportunities}")

                if opportunities:
                    print(
                        f"🤖 [DECISION] Consultation IA pour {len(opportunities)} assets..."
                    )
                    ai_response = self.ai_interface.request_ia_advice(
                        opportunities, analyzed_context
                    )
                    analyzed_context["ai_advice"] = ai_response
                    analyzed_context["ai_recommendation_score"] = ai_response.get(
                        "ai_vote_for_configs", {}
                    )
                    print(f"🤖 [DECISION] IA consultée avec succès")
                else:
                    print(f"🤖 [DECISION] Aucun asset sélectionné pour l'IA")
            else:
                print(f"🤖 [DECISION] IA désactivée")

            # 3. Sélection de la stratégie optimale
            print(f"🤖 [DECISION] Étape 3: Sélection de stratégie...")
            # CORRECTION : Utilise self.strategy_manager.strategy_registry comme source de vérité
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
                    "Aucune stratégie optimale sélectionnée pour ce cycle. Pipeline de décision s'arrête."
                )
                print(f"🤖 [DECISION] ❌ Aucune stratégie optimale trouvée")
                return {
                    "context": analyzed_context,
                    "config_used": self.config_manager.get_current_dynamic_config(),
                    "final_decision": {},
                }

            print(
                f"🤖 [DECISION] ✅ Stratégie optimale sélectionnée: {optimal_config.get('strategy_name', 'Unknown')}"
            )

            # 4. Adaptation de la configuration pour le cycle actuel
            print(f"🤖 [DECISION] Étape 4: Adaptation de configuration...")
            # On fusionne la config de base avec la config de la stratégie choisie
            config_for_this_cycle = self.config_manager._merge_dicts(
                self.config_manager.get_current_dynamic_config(), optimal_config
            )
            adapted_config = self.adapt_config(config_for_this_cycle, analyzed_context)
            print(f"🤖 [DECISION] Configuration adaptée avec succès")

            # 5. Décision de trade finale basée sur la stratégie et la configuration adaptées
            print(f"🤖 [DECISION] Étape 5: Décision de trade finale...")
            signals = analyzed_context.get("trading_signals", {})
            print(f"🤖 [DECISION] Signaux disponibles: {list(signals.keys())}")

            trade_decision = self.decide_trade_to_execute(
                analyzed_context,
                adapted_config,  # Utilise la configuration spécifiquement adaptée pour ce cycle
                signals,
            )

            print(
                f"🤖 [DECISION] Décision finale: {trade_decision.get('action', 'AUCUNE')}"
            )

            return {
                "timestamp_utc": datetime.now(UTC).isoformat(),
                "context": analyzed_context,
                "config_used": adapted_config,
                "final_decision": trade_decision,
            }

        except Exception as e:
            print(f"💥 [DECISION] ERREUR dans le pipeline: {e}")
            self.logger.error(
                f"Erreur critique dans institutional_decision_pipeline: {e}",
                exc_info=True,
            )
            return {
                "context": context,
                "config_used": self.config_manager.get_current_dynamic_config(),
                "final_decision": {},
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
        🎯 ÉVALUATION CONDITIONS SCALPING PAR ASSET (version corrigée)
        - Volatilité lue en priorité depuis volatility_pct / volatility_percentage
        - Fallback sur 'volatility' avec détection d'unité (décimal vs pourcentage)
        - Seuils MTF 100% configurables via prod_config.json
        """
        if not signals or not isinstance(signals, dict):
            return 0.0

        condition_score = 0.0
        print(f"  🔍 [{asset}] Analyse conditions scalping...")

        # === 1. SIGNAUX KATANA PRIORITAIRES (identique) ===
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

        # === 2. CONDITIONS MTF (corrigées: unités & seuils dynamiques) ===
        # 2.1 Volatilité (% cohérente)
        # Priorité aux champs déjà en pourcentage
        vol_pct = None
        if isinstance(signals.get("volatility_pct"), (int, float)):
            vol_pct = float(signals.get("volatility_pct"))
        elif isinstance(signals.get("volatility_percentage"), (int, float)):
            vol_pct = float(signals.get("volatility_percentage"))
        else:
            # Fallback : essayer "volatility" et déduire l'unité
            vol_raw = signals.get("volatility")
            if isinstance(vol_raw, (int, float)):
                vol_raw = float(vol_raw)
                # Heuristique: si <= 1, on assume décimal (0.0008 => 0.08%)
                vol_pct = vol_raw * 100.0 if vol_raw <= 1.0 else vol_raw
            else:
                vol_pct = 0.0

        # 2.2 Spread (points)
        spread_points = signals.get("current_spread_points")
        if not isinstance(spread_points, (int, float)):
            spread_points = signals.get("spread", float("inf"))
        try:
            spread_points = float(spread_points)
        except Exception:
            spread_points = float("inf")

        # 2.3 Volume z-score
        volume_zscore = signals.get("volume_zscore", 0.0)
        try:
            volume_zscore = float(volume_zscore)
        except Exception:
            volume_zscore = 0.0

        # 2.4 Seuils MTF dynamiques (prod_config.json)
        #   scoring_rules.scalping.mtf.min_volatility_pct  (ex: 0.01 pour 0,01%)
        #   scoring_rules.scalping.mtf.max_spread_points   (ex: 50)
        #   scoring_rules.scalping.mtf.min_volume_zscore   (ex: 0.5)
        volatility_threshold_pct = self.config_manager.get(
            "scoring_rules.scalping.mtf.min_volatility_pct", 0.01
        )
        max_spread_points = self.config_manager.get(
            "scoring_rules.scalping.mtf.max_spread_points", 50
        )
        min_volume_zscore = self.config_manager.get(
            "scoring_rules.scalping.mtf.min_volume_zscore", 0.5
        )

        mtf_conditions_met = 0
        mtf_total_conditions = 3

        # Volatilité en % : log clair
        if vol_pct >= volatility_threshold_pct:
            mtf_conditions_met += 1
            print(
                f"    ✅ Volatilité OK: {vol_pct:.3f}% >= {volatility_threshold_pct:.3f}%"
            )
        else:
            print(
                f"    ❌ Volatilité faible: {vol_pct:.3f}% < {volatility_threshold_pct:.3f}%"
            )

        # Spread en points
        if spread_points <= max_spread_points:
            mtf_conditions_met += 1
            print(f"    ✅ Spread OK: {spread_points:.0f} <= {max_spread_points}")
        else:
            print(f"    ❌ Spread élevé: {spread_points:.0f} > {max_spread_points}")

        # Volume z-score
        if volume_zscore >= min_volume_zscore:
            mtf_conditions_met += 1
            print(f"    ✅ Volume OK: {volume_zscore:.2f} >= {min_volume_zscore}")
        else:
            print(f"    ❌ Volume faible: {volume_zscore:.2f} < {min_volume_zscore}")

        # Bonus MTF proportionnel (inchangé)
        mtf_score = (mtf_conditions_met / mtf_total_conditions) * 0.3
        condition_score += mtf_score
        print(
            f"    🔄 Conditions MTF: {mtf_conditions_met}/{mtf_total_conditions} -> +{mtf_score:.3f}"
        )

        # === 3. PHASES SCALPING SPÉCIFIQUES (identique) ===
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
                phase_score = weight * 0.25  # 25% du score pour la phase
                print(f"    ⚡ Phase scalping: {current_phase} -> +{phase_score:.3f}")
                break
        condition_score += phase_score

        # === 4. QUALITÉ ET CONFIANCE (identique) ===
        confidence = signals.get("confidence_score", 0.0)
        is_liquid = signals.get("is_liquid", False)

        if confidence > 0.7:
            confidence_bonus = 0.15
            condition_score += confidence_bonus
            print(
                f"    📈 Haute confiance: {confidence:.3f} -> +{confidence_bonus:.3f}"
            )
        elif confidence > 0.5:
            confidence_bonus = 0.05
            condition_score += confidence_bonus
            print(f"    📊 Confiance OK: {confidence:.3f} -> +{confidence_bonus:.3f}")

        if is_liquid:
            liquidity_bonus = 0.1
            condition_score += liquidity_bonus
            print(f"    💧 Asset liquide -> +{liquidity_bonus:.3f}")

        # === 5. CONFLUENCE MTF (identique) ===
        multi_tf_enabled = signals.get("multi_tf_enabled", False)
        confluence_score = signals.get("confluence_score", 0.0)

        if multi_tf_enabled and confluence_score > 0.7:
            mtf_bonus = 0.2
            condition_score += mtf_bonus
            print(
                f"    🔥 MTF Confluence élevée: {confluence_score:.3f} -> +{mtf_bonus:.3f}"
            )
        elif multi_tf_enabled and confluence_score > 0.5:
            mtf_bonus = 0.1
            condition_score += mtf_bonus
            print(
                f"    🔄 MTF Confluence OK: {confluence_score:.3f} -> +{mtf_bonus:.3f}"
            )

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
        Orchestre la décision de sortie en déléguant l'évaluation des positions ouvertes
        aux instances des stratégies qui les ont ouvertes, identifiées par leur 'magic number'.
        Déplacée de ConfigManager.

        Args:
            context (Dict): Le contexte de marché et système complet.
            open_positions (List[Dict[str, Any]]): La liste des positions actuellement ouvertes.
            active_config (Dict): La configuration de la stratégie active (utilisée comme fallback).
            strategy_manager_instance: L'instance du StrategyManager.

        Returns:
            List[Dict[str, Any]]: Une liste consolidée de toutes les décisions de sortie provenant des stratégies actives.
        """
        self.logger.info(
            "Orchestration de la décision de sortie en déléguant aux stratégies actives..."
        )
        all_exit_decisions = []

        positions_by_strategy = {}
        for pos in open_positions:
            magic = pos.get("magic")
            if magic:
                positions_by_strategy.setdefault(magic, []).append(pos)

        if not positions_by_strategy:
            self.logger.debug("Aucune position avec un magic number à évaluer.")
            return []

        # Utilise StrategyManager pour accéder au _config_knowledge_base et aux classes de stratégie
        # (Une fois StrategyManager refactorisé, _config_knowledge_base sera un attribut de celui-ci)
        if strategy_manager_instance:
            magic_to_strategy_map = (
                strategy_manager_instance.get_magic_to_strategy_map()
            )  # Nouvelle méthode à créer dans StrategyManager
        else:  # Fallback si StrategyManager n'est pas encore injecté ou fonctionnel
            self.logger.warning(
                "StrategyManager non injecté. La logique de décision de sortie pourrait être limitée."
            )
            magic_to_strategy_map = {}
            # Accès temporaire à l'attribut du ConfigManager pour la démo
            if hasattr(self.config_manager, "_config_knowledge_base"):
                for config_data in self.config_manager._config_knowledge_base.values():
                    content = config_data.get("content", {})
                    magic = content.get("magic_number")
                    strategy_class = config_data.get("strategy_class")
                    if magic and strategy_class:
                        magic_to_strategy_map[magic] = {
                            "class": strategy_class,
                            "config": content,
                        }

        for magic, positions in positions_by_strategy.items():
            if magic in magic_to_strategy_map:
                strategy_info = magic_to_strategy_map[magic]
                strategy_class = strategy_info["class"]
                strategy_config = strategy_info["config"]
                strategy_name = strategy_config.get("strategy_name", "Unknown")

                self.logger.info(
                    f"Évaluation des sorties pour {len(positions)} position(s) de la stratégie '{strategy_name}' (Magic: {magic})."
                )
                try:
                    # Passe l'instance de ConfigManager à la stratégie
                    strategy_instance = strategy_class(
                        config_manager_instance=self.config_manager,
                        strategy_config=strategy_config,
                    )
                    exit_decisions_for_strategy = strategy_instance.evaluate_exit(
                        context, positions
                    )

                    if exit_decisions_for_strategy:
                        all_exit_decisions.extend(exit_decisions_for_strategy)
                        self.logger.info(
                            f"{len(exit_decisions_for_strategy)} décision(s) de sortie retournée(s) par la stratégie '{strategy_name}'."
                        )

                except Exception as e:
                    self.logger.error(
                        f"Erreur lors de l'évaluation des sorties pour la stratégie '{strategy_name}': {e}",
                        exc_info=True,
                    )
            else:
                self.logger.warning(
                    f"Aucune stratégie trouvée pour le magic number {magic}. Les {len(positions)} position(s) associées ne peuvent pas être gérées pour la sortie."
                )

        return all_exit_decisions

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
        # Normalise l'action
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

        # Normalise/Sécurise l’asset
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

        # Pose un order_type par défaut si absent
        order_type = str(trade_decision.get("order_type", "MARKET")).upper()
        if order_type not in {
            "MARKET",
            "BUY_LIMIT",
            "SELL_LIMIT",
            "BUY_STOP",
            "SELL_STOP",
        }:
            # On force MARKET si valeur exotique, pour éviter de bloquer ici (les validations fines se font plus tard)
            self.logger.debug(f"order_type inconnu '{order_type}', fallback 'MARKET'.")
            order_type = "MARKET"

        # Réinjecte les valeurs normalisées dans la décision
        trade_decision["action"] = normalized_action
        trade_decision["asset"] = asset_raw
        trade_decision["order_type"] = order_type

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

        # calculate_risk_parameters est une méthode de DecisionPipeline
        risk_params = self.calculate_risk_parameters(
            context, current_config, trade_decision
        )
        self.logger.debug(f"Paramètres de risque calculés: {risk_params}")

        if not risk_params.get("volume", 0.0) > 0:
            self.logger.warning(
                "Calcul de risque invalide ou volume nul. Trade annulé."
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
        CORE évalue directement les signaux et prend la décision finale.
        Utilise les paramètres de stratégie mais applique une logique décisionnelle centralisée.
        """
        self.logger.info(
            f"🔍 CORE analyse {len(signals)} assets avec paramètres {strategy_name}"
        )

        best_asset = None
        best_score = 0.0
        best_signals = None

        # Seuils de validation CORE (plus flexibles que les stratégies)
        min_confidence = config.get("min_confidence", 0.65)  # Plus bas que 0.77

        for asset, asset_signals in signals.items():
            confidence = asset_signals.get("confidence_score", 0.0)
            phase = asset_signals.get("phase", "")

            self.logger.debug(
                f"🔍 [{asset}] Confiance: {confidence:.3f}, Phase: {phase}"
            )

            # NOUVELLE LOGIQUE CORE : Plus permissive
            score = confidence

            # Bonus selon les signaux détectés
            if asset_signals.get("ob_detected", False):
                score += 0.1
                self.logger.debug(f"    ✅ Order Block détecté -> +0.1")

            if asset_signals.get("fvg_detected", False):
                score += 0.1
                self.logger.debug(f"    ✅ FVG détecté -> +0.1")

            if asset_signals.get("bos_mss_detected", False):
                score += 0.15
                self.logger.debug(f"    ✅ BOS/MSS détecté -> +0.15")

            # Validation CORE : Accepter si confiance suffisante OU phase conclusive
            is_valid = False

            if confidence >= min_confidence:
                is_valid = True
                self.logger.info(
                    f"✅ [{asset}] Accepté par CORE - Confiance {confidence:.3f} >= {min_confidence}"
                )
            elif confidence >= 0.5 and any(
                keyword in phase.lower()
                for keyword in ["bullish", "bearish", "trending"]
            ):
                is_valid = True
                self.logger.info(
                    f"✅ [{asset}] Accepté par CORE - Phase conclusive: {phase}"
                )
            else:
                self.logger.debug(
                    f"❌ [{asset}] Rejeté - Confiance {confidence:.3f} et phase {phase} insuffisantes"
                )

            if is_valid and score > best_score:
                best_score = score
                best_asset = asset
                best_signals = asset_signals

        if not best_asset:
            self.logger.info("❌ CORE: Aucun asset ne respecte les critères d'entrée")
            return {}

        self.logger.info(f"🎯 CORE sélectionne: {best_asset} (score: {best_score:.3f})")

        # Construire la décision de trade
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
        CORE construit la décision finale de trade basée sur les signaux.
        """
        phase = signals.get("phase", "")
        # Utiliser le prix de clôture de la dernière bougie comme prix d'entrée
        current_price = signals.get("close", 0)

        if not current_price or current_price <= 0:
            self.logger.error(f"❌ Prix actuel manquant ou invalide pour {asset}")
            return {}

        # Déterminer la direction (logique simplifiée mais robuste)
        action = None

        # Priorité aux phases claires
        if any(
            keyword in phase.lower() for keyword in ["bullish", "up", "accumulation"]
        ):
            action = "BUY"
        elif any(
            keyword in phase.lower() for keyword in ["bearish", "down", "distribution"]
        ):
            action = "SELL"
        else:
            # Si la phase est 'no_clear_phase', on se base sur le momentum comme fallback
            self.logger.warning(
                f"Phase '{phase}' non conclusive. Tentative de décision basée sur le momentum."
            )
            volume_momentum = signals.get("volume_momentum", 0)
            if volume_momentum > 0.1:  # Seuil pour éviter le bruit
                action = "BUY"
            elif volume_momentum < -0.1:
                action = "SELL"

        if not action:
            self.logger.warning(
                f"❌ Direction de trade indéterminée pour {asset} (Phase: {phase}, Momentum: {signals.get('volume_momentum', 0):.2f})"
            )
            return {}

        # Paramètres SL/TP depuis la configuration de la stratégie active
        sl_pips = config.get("stop_loss_pips", 20)
        tp_pips = config.get("take_profit_pips", 40)
        magic_number = config.get("magic_number", 999999)

        # ======================= LA CORRECTION CLÉ EST ICI =======================
        # On s'assure que le dictionnaire final contient TOUTES les clés attendues
        # par le TradeExecutor, notamment "action".
        trade_decision = {
            "action": action,  # <-- LA CLÉ MANQUANTE EST AJOUTÉE ICI !
            "asset": asset,
            "strategy_type": f"core_{config.get('strategy_name', 'decision')}",
            "entry_price": current_price,
            "target_sl_pips": sl_pips,
            "target_tp_pips": tp_pips,
            "rule_name": f"core_decision_{phase}",
            "confidence": signals.get("confidence_score", 0.0),
            "timestamp": context.get("current_time_utc", datetime.now(UTC).isoformat()),
            "magic_number": magic_number,
        }
        # =======================================================================

        self.logger.info(
            f"✅ CORE construit trade {action} {asset} @ {current_price} (SL: {sl_pips}, TP: {tp_pips})"
        )

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

    def calculate_risk_parameters(
        self,
        context: Dict[str, Any],
        config: Dict[str, Any],
        trade_decision: Dict[str, Any],
    ) -> Dict[str, Any]:
        """
        Calcule les paramètres de risque dynamiques (taille de lot, etc.) pour un trade.
        Déplacée de ConfigManager.
        """
        self.logger.info(
            f"Calcul des paramètres de risque pour {trade_decision.get('asset')}..."
        )

        equity = context.get("account_info", {}).get(
            "equity",
            self.config_manager.get(
                "risk_management_settings.default_account_equity", 10000.0
            ),
        )
        if equity <= 0:
            self.logger.error(
                f"Équité du compte ({equity}) non positive. Impossible de calculer le risque."
            )
            return {}

        risk_per_trade_percent = config.get("risk_per_trade_percent", 1.0)
        if not (0 < risk_per_trade_percent <= 100):
            self.logger.error(
                f"Pourcentage de risque par trade invalide ({risk_per_trade_percent}%)."
            )
            return {}
        max_dollar_risk = equity * (risk_per_trade_percent / 100)

        asset = trade_decision.get("asset", "UNKNOWN_ASSET")

        active_broker_account = context.get("active_broker_account", {})
        account_trade_settings = active_broker_account.get("trade_settings", {})

        asset_mt5_info = (
            context.get("market_data", {}).get(asset, {}).get("symbol_info", {})
        )

        point = asset_mt5_info.get(
            "point",
            self.config_manager.get(
                "risk_management_settings.default_points_in_pip", 0.00001
            ),
        )
        contract_size = asset_mt5_info.get(
            "trade_contract_size",
            self.config_manager.get(
                "risk_management_settings.default_contract_size", 100000
            ),
        )

        if point <= 0 or contract_size <= 0:
            self.logger.error(
                f"Informations cruciales du symbole manquantes ou invalides (point={point}, contract_size={contract_size}) pour {asset}. Impossible de calculer le risque."
            )
            return {}

        target_sl_pips = trade_decision.get("target_sl_pips")
        if target_sl_pips is None or target_sl_pips <= 0:
            self.logger.error(
                f"Stop loss invalide ou nul ({target_sl_pips} pips) pour {asset}. Impossible de calculer le volume. Ordre bloqué pour sécurité."
            )
            return {
                "volume": 0.0,
                "max_dollar_risk": 0.0,
            }

        sl_distance_in_price = target_sl_pips * point
        dollar_risk_per_lot_estimated = sl_distance_in_price * contract_size

        if dollar_risk_per_lot_estimated <= 0:
            self.logger.warning(
                f"Le risque par lot estimé est nul ou négatif pour {asset}. Utilisation du volume minimum pour cette estimation."
            )
            dollar_risk_per_lot_estimated = self.config_manager.get(
                "risk_management_settings.min_dollar_risk_per_lot_fallback", 1.0
            )

        calculated_lot_size = max_dollar_risk / dollar_risk_per_lot_estimated

        min_lot_size = account_trade_settings.get(
            "min_lot",
            self.config_manager.get(
                "risk_management_settings.min_lot_size_fallback", 0.01
            ),
        )
        max_lot_size = account_trade_settings.get(
            "max_lot",
            self.config_manager.get("global_safety.max_allowed_lot_size", 50.0),
        )
        lot_step = account_trade_settings.get(
            "lot_step",
            self.config_manager.get(
                "risk_management_settings.default_lot_step_fallback", 0.01
            ),
        )

        if lot_step <= 0:
            self.logger.error(
                f"Lot step invalide ou nul ({lot_step}) pour {asset}. Utilisation du fallback 0.01."
            )
            lot_step = 0.01

        volume = max(min_lot_size, calculated_lot_size)
        volume = min(max_lot_size, volume)

        volume = round(volume / lot_step) * lot_step

        lot_size_precision = (
            len(str(lot_step).split(".")[-1]) if "." in str(lot_step) else 0
        )
        final_volume = round(volume, lot_size_precision)

        self.logger.info(
            f"Calcul de risque pour {asset}: Equity=${equity:.2f}, Risque={risk_per_trade_percent}%, Max Dollar Risque=${max_dollar_risk:.2f}, Volume Final={final_volume:.{lot_size_precision}f} (Risque Estimé par Lot=${dollar_risk_per_lot_estimated:.2f})."
        )

        return {
            "volume": final_volume,
            "max_dollar_risk": max_dollar_risk,
            "risk_per_trade_percent": risk_per_trade_percent,
        }
