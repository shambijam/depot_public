# core/ai_interface.py

import logging
import json
import hashlib
import pandas as pd
from datetime import datetime, UTC
from pathlib import Path
from typing import Dict, Any, List, Optional, Tuple

# Import pour la bibliothèque de tokenisation, avec un fallback si elle n'est pas disponible.
try:
    import tiktoken
    TIKTOKEN_AVAILABLE = True
except ImportError:
    TIKTOKEN_AVAILABLE = False

logger = logging.getLogger(__name__)

class AIInterface:
    """
    Gère toutes les fonctionnalités liées à l'interaction avec le module AIDecision.
    Cette classe est responsable de la construction des prompts, de la gestion
    du cache des réponses AI, et de l'orchestration des requêtes vers le modèle AI.
    Elle n'est pas un décideur, mais une interface pour le conseiller AI.
    """

    def __init__(self, config_manager_instance=None, ai_decision_instance=None):
        """
        Initialise l'interface AI.

        Args:
            config_manager_instance: L'instance du ConfigManager pour accéder aux configurations.
            ai_decision_instance: L'instance du module AIDecision pour interagir avec le modèle AI.
        """
        self.config_manager = config_manager_instance
        self.ai_decision_instance = ai_decision_instance
        self.logger = logging.getLogger(__name__)

        # Initialisation du cache pour les réponses AI
        # Tuple[Dict[str, Any], datetime] stocke la réponse et son timestamp d'ajout au cache
        self._ai_advice_cache: Dict[str, Tuple[Dict[str, Any], datetime]] = {}

        if self.config_manager:
            # Assurez-vous que ces chemins et paramètres sont accessibles via ConfigManager
            self.prompts_base_dir = self.config_manager.get("paths.prompts", "config/prompts/")
            self.prompts_file_name = self.config_manager.get("ai.prompt_settings.prompts_file_name", "prompts.yaml")
        else:
            self.logger.warning("AIInterface initialisé sans ConfigManager. Certains chemins et paramètres pourraient ne pas être dynamiques.")
            self.prompts_base_dir = "config/prompts/"
            self.prompts_file_name = "prompts.yaml"

        self.prompts = {}
        self._load_prompts()  # Charger les prompts au démarrage de l'interface

    def _load_prompts(self) -> None:
        """
        Charge les templates de prompts pour l'IA depuis le fichier YAML spécifié.
        Déplacée de AIDecision et adaptée pour AIInterface.
        """
        try:
            import yaml  # Import local

            full_prompts_path = Path(self.prompts_base_dir) / self.prompts_file_name
            with open(full_prompts_path, "r", encoding="utf-8") as f:
                self.prompts = yaml.safe_load(f)
            self.logger.info(f"AIInterface: Prompts chargés avec succès depuis '{full_prompts_path}'.")
        except FileNotFoundError as e:
            self.logger.critical(
                f"AIInterface: Fichier de prompts '{full_prompts_path}' non trouvé. Les fonctions IA basées sur les prompts ne fonctionneront pas."
            )
            self.prompts = {}
            if self.config_manager:
                self.config_manager.send_alert(
                    "CRITIQUE",
                    f"AI Prompts Manquants: {full_prompts_path}",
                    alert_type="telegram_critical",
                )
            raise e  # Relance l'exception car c'est critique
        except Exception as e:
            self.logger.critical(
                f"AIInterface: Erreur lors du chargement de '{full_prompts_path}': {e}. Les fonctions IA basées sur les prompts ne fonctionneront pas.",
                exc_info=True,
            )
            self.prompts = {}
            if self.config_manager:
                self.config_manager.send_alert(
                    "CRITIQUE",
                    f"AI Prompts Erreur Chargement: {e}",
                    alert_type="telegram_critical",
                )
            raise e  # Relance l'exception car c'est critique

    def build_ia_prompt(self, opportunities: List[str], context: Dict[str, Any]) -> str:
        # AJOUT DEBUG AU DÉBUT
        self.logger.info(f"🔍 DEBUG - Opportunities: {opportunities}")
        self.logger.info(f"🔍 DEBUG - Context keys: {list(context.keys())}")
        if 'market_data' in context:
            self.logger.info(f"🔍 DEBUG - Market data assets: {list(context['market_data'].keys())}")
        
        # ... reste du code existant
        """
        Génère un prompt pour l'IA en assemblant des templates de la configuration.
        Cette méthode construit un prompt contextuel et token-conscient en utilisant des
        modèles de texte (templates) chargés depuis la configuration, garantissant
        une flexibilité totale sans modifier le code.

        Args:
            opportunities (List[str]): La shortlist des symboles d'actifs à analyser.
            context (Dict): Le contexte système et marché actuel (enrichi).

        Returns:
            str: Le prompt formaté, prêt à être envoyé à l'IA.
        """
        self.logger.info("Construction du prompt pour l'IA via le moteur de templates...")

        if not opportunities:
            self.logger.warning("Impossible de construire le prompt : la liste d'opportunités est vide.")
            return ""

        # ZÉRO HARD CODING : Tous les textes sont chargés depuis la configuration.
        # Utilisation de self.config_manager.get() pour récupérer les prompts.
        # Les valeurs par défaut sont ici pour la résilience si config_manager n'est pas parfait.
        role_def = self.config_manager.get(
            "prompts.advisor_shortlist.role_definition",
            "Vous êtes un analyste institutionnel senior. "
        )
        context_tpl = self.config_manager.get(
            "prompts.advisor_shortlist.global_context_template",
            "\n## Contexte Global\n- Régime: {market_regime}\n- VIX: {vix}\n"
        )
        # La tâche de l'IA est maintenant de fournir un diagnostic et non un niveau_confiance qui serait un score de décision.
        task_def = self.config_manager.get(
            "prompts.advisor_shortlist.task_definition",
            "\n## Votre Tâche\nFournissez un 'diagnostic' pour chaque actif et indiquez une 'pertinence_strategique' (Faible, Moyenne, Élevée) en fonction de l'opportunité de trading. "
        )

        prompt_parts = [role_def]
        prompt_parts.append(
            context_tpl.format(
                market_regime=context.get("current_market_regime", "inconnu"),
                vix=context.get("vix_index", "N/A"),
            )
        )
        prompt_parts.append("## Analyse des Opportunités\n")

        max_tokens = self.config_manager.get("ai.generation_params.max_tokens", 4096)
        ideal_tokens_ratio = self.config_manager.get("ai.generation_params.ideal_tokens_ratio", 0.75)
        ideal_tokens = int(max_tokens * ideal_tokens_ratio)

        for asset in opportunities:
                    # Récupérer les données de marché et de signaux annotées par PhaseObserver
                    asset_market_data_full = context.get("market_data", {}).get(asset, {})
                    
                    # CORRECTION: Récupérer le DataFrame depuis la clé 'annotated_rates_df'
                    annotated_df = None
                    if isinstance(asset_market_data_full, dict):
                        # D'abord essayer annotated_rates_df, puis les autres clés possibles
                        annotated_df = (asset_market_data_full.get("annotated_rates_df") or 
                                    asset_market_data_full.get("ohlcv_data") or 
                                    asset_market_data_full.get("data"))
                    
                    # Obtenir les données pour le prompt
                    if "market_data_summary" in context:
                        # Si un résumé existe, l'utiliser en priorité
                        asset_market_data = context["market_data_summary"].get(asset, {})
                    elif annotated_df is not None and hasattr(annotated_df, 'iloc') and not annotated_df.empty:
                        # Si c'est un DataFrame non vide, prendre la dernière ligne
                        asset_market_data = annotated_df.iloc[-1].to_dict()
                        self.logger.debug(f"🔍 DEBUG - Utilisation DataFrame pour {asset}: {len(annotated_df)} lignes")
                    else:
                        self.logger.warning(f"Données de marché pour l'actif {asset} non trouvées ou malformées pour le prompt IA.")
                        continue  # Passer cet actif si les données sont invalides

                    # V-- LA LOGIQUE DE CRÉATION DU BLOC D'ACTIF POUR LE PROMPT --V
                    asset_info = [f"### Actif: {asset}"]
                    asset_info.append(f"- Prix Actuel: {asset_market_data.get('close', 'N/A'):.5f}")
                    # Suppression de la "Confiance" de la Phase de Marché dans le prompt de l'IA
                    asset_info.append(f"- Phase de Marché: {asset_market_data.get('phase', 'N/A')}")

                    # Signaux de confirmation chirurgicaux (si détectés)
                    if asset_market_data.get("entry_confirmation_bullish"):
                        asset_info.append(f"- Signal Chirurgical: Confirmation d'entrée HAUSSIÈRE.")
                    elif asset_market_data.get("entry_confirmation_bearish"):
                        asset_info.append(f"- Signal Chirurgical: Confirmation d'entrée BAISSIÈRE.")

                    # Détails des anomalies de volume
                    if asset_market_data.get("volume_anomaly_details"):
                        vol_details = asset_market_data["volume_anomaly_details"]
                        asset_info.append(f"- Anomalie Volume: {vol_details.get('type')} (Z-score: {vol_details.get('z_score', 'N/A'):.2f}, Momentum: {vol_details.get('volume_momentum', 'N/A'):.2f})")

                    # Proximité de la liquidité
                    if asset_market_data.get("nearest_liquidity_level_details"):
                        liq_details = asset_market_data["nearest_liquidity_level_details"]
                        asset_info.append(f"- Proximité Liquidité: Niveau {liq_details.get('type')} à {liq_details.get('level', 'N/A'):.5f} ({liq_details.get('distance_pips', 'N/A'):.2f} pips).")

                    # Informations sur les Order Blocks validés
                    if asset_market_data.get("validated_ob"):
                        ob_details = asset_market_data.get("ob_details", {})
                        asset_info.append(f"- Order Block Validé: Type {ob_details.get('type')}, Zone [{ob_details.get('bottom'):.5f}-{ob_details.get('top'):.5f}].")

                    # Informations sur les FVG
                    if asset_market_data.get("fvg_details"):
                        fvg_details = asset_market_data["fvg_details"]
                        asset_info.append(f"- Fair Value Gap: Type {fvg_details.get('type')}, Zone [{fvg_details.get('bottom'):.5f}-{fvg_details.get('top'):.5f}].")

                    # Informations sur les BOS/MSS
                    if asset_market_data.get("bos_mss_details"):
                        bos_details = asset_market_data["bos_mss_details"]
                        asset_info.append(f"- Rupture de Structure: Type {bos_details.get('type')}, Niveau {bos_details.get('level_broken'):.5f}.")

                    # Action de Prix Récente - CORRECTION: utiliser le DataFrame récupéré
                    recent_price_data_points = self.config_manager.get("ai.prompt_settings.recent_price_data_points", 10)
                    recent_closes_list = []
                    if annotated_df is not None and hasattr(annotated_df, 'iloc') and 'close' in annotated_df.columns:
                        recent_closes_list = annotated_df['close'].tail(recent_price_data_points).tolist()
                    
                    if recent_closes_list:
                        asset_info.append(f"- Action de Prix Récente ({recent_price_data_points}p): {', '.join([f'{p:.5f}' for p in recent_closes_list])}")

                    asset_block = "\n".join(asset_info)
                    # A-- FIN DE LA LOGIQUE DE CRÉATION --A

                    # Vérification des tokens avant ajout
                    current_prompt_estimate = self._estimate_tokens("\n".join(prompt_parts) + "\n" + asset_block)
                    if current_prompt_estimate > ideal_tokens:
                        self.logger.warning(f"Limite de tokens atteinte ({current_prompt_estimate}/{ideal_tokens}), l'actif {asset} n'est pas inclus dans le prompt.")
                        break
                    prompt_parts.append(asset_block)

        prompt_parts.append(task_def)
        final_prompt = "\n".join(prompt_parts)

        # Vérification finale
        if self._estimate_tokens(final_prompt) > max_tokens:
            self.logger.error(f"Le prompt final dépasse la limite de tokens ({max_tokens}). L'analyse IA pourrait être incomplète.")
            final_prompt = final_prompt[:max_tokens]  # Tronquer à la limite absolue si dépasse

        self.logger.debug(f"Prompt IA généré (longueur: {len(final_prompt)} caractères).")
        return final_prompt
    
    def _construire_bloc_actif(self, asset: str, context: Dict[str, Any]) -> Optional[str]:
        """
        Construit le bloc d'information pour un actif avec validation robuste.
        CORRECTION: Amélioration de la récupération des données
        """
        try:
            # 🆕 DEBUG: Ajouter des logs pour comprendre la structure
            self.logger.debug(f"🔍 DEBUG - Construction bloc pour {asset}")
            self.logger.debug(f"🔍 DEBUG - Clés contexte: {list(context.keys())}")
            
            if "market_data" in context:
                self.logger.debug(f"🔍 DEBUG - Assets dans market_data: {list(context['market_data'].keys())}")
            
            # Récupération des données avec multiples sources (AMÉLIORÉE)
            asset_data = self._recuperer_donnees_actif(asset, context)
            
            if not asset_data:
                self.logger.warning(f"❌ Aucune donnée trouvée pour {asset}")
                # 🆕 AMÉLIORATION: Bloc minimal mais informatif
                return f"""### Actif: {asset}
            - Statut: Données de marché indisponibles
            - Phase: En attente d'analyse
            - Action: Surveillance passive recommandée"""

            # Construction du bloc avec les données disponibles
            asset_info = [f"### Actif: {asset}"]
            
            # Prix actuel (avec fallbacks multiples)
            prix = None
            for prix_key in ['close', 'price', 'current_price', 'last_price']:
                if prix_key in asset_data and asset_data[prix_key] is not None:
                    prix = asset_data[prix_key]
                    break
            
            if prix is not None:
                asset_info.append(f"- Prix Actuel: {float(prix):.5f}")
            else:
                asset_info.append(f"- Prix Actuel: Non disponible")

            # Phase de marché (avec fallbacks)
            phase = None
            for phase_key in ['phase', 'market_phase', 'trend_phase']:
                if phase_key in asset_data and asset_data[phase_key]:
                    phase = asset_data[phase_key]
                    break
            
            asset_info.append(f"- Phase de Marché: {phase or 'Non déterminée'}")

            # 🆕 AJOUT: Informations supplémentaires si disponibles
            if 'confidence' in asset_data:
                asset_info.append(f"- Confiance Analyse: {asset_data['confidence']:.2f}")
            
            if 'volume' in asset_data:
                asset_info.append(f"- Volume: {asset_data['volume']}")
            
            # Ajout des signaux disponibles
            self._ajouter_signaux_disponibles(asset_info, asset_data)

            result = "\n".join(asset_info)
            self.logger.debug(f"✅ Bloc construit pour {asset}: {len(result)} caractères")
            return result
            
        except Exception as e:
            self.logger.error(f"❌ Erreur construction bloc {asset}: {e}")
            return self._bloc_actif_minimal(asset)

   # Ajoutez cette méthode de debug dans ai_interface.py

    def _recuperer_donnees_actif(self, asset: str, context: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """
        MÉTHODE DE DEBUG APPROFONDI pour comprendre pourquoi EURUSD n'est pas trouvé
        """
        self.logger.info(f"🔍 DEBUG DÉTAILLÉ - Récupération données pour {asset}")
        
        # DEBUG: Vérifier toutes les sources possibles
        self.logger.info(f"📊 Context keys: {list(context.keys())}")
        
        # Source 1: market_data_summary
        if "market_data_summary" in context:
            summary_keys = list(context["market_data_summary"].keys()) if isinstance(context["market_data_summary"], dict) else "NOT_DICT"
            self.logger.info(f"📊 market_data_summary keys: {summary_keys}")
            
            if isinstance(context["market_data_summary"], dict) and asset in context["market_data_summary"]:
                data = context["market_data_summary"][asset]
                self.logger.info(f"✅ TROUVÉ dans market_data_summary!")
                self.logger.info(f"📄 Type: {type(data)}, Keys: {list(data.keys()) if isinstance(data, dict) else 'NOT_DICT'}")
                return data
            else:
                self.logger.info(f"❌ {asset} pas dans market_data_summary")
                
        # Source 2: market_data (DataFrame ou dict)
        if "market_data" in context:
            market_data = context["market_data"]
            market_keys = list(market_data.keys()) if isinstance(market_data, dict) else "NOT_DICT"
            self.logger.info(f"📊 market_data keys: {market_keys}")
            self.logger.info(f"📊 market_data type: {type(market_data)}")
            
            if isinstance(market_data, dict) and asset in market_data:
                data = market_data[asset]
                self.logger.info(f"✅ TROUVÉ dans market_data!")
                self.logger.info(f"📄 Type de données: {type(data)}")
                
                if isinstance(data, pd.DataFrame):
                    self.logger.info(f"📊 DataFrame shape: {data.shape}")
                    self.logger.info(f"📊 DataFrame columns: {list(data.columns)}")
                    if not data.empty:
                        last_row = data.iloc[-1]
                        result = last_row.to_dict()
                        self.logger.info(f"📄 Dernière ligne convertie: {list(result.keys())}")
                        self.logger.info(f"📄 Valeurs échantillon: {dict(list(result.items())[:5])}")
                        return result
                    else:
                        self.logger.warning(f"❌ DataFrame vide pour {asset}")
                elif isinstance(data, dict):
                    self.logger.info(f"📄 Dict direct keys: {list(data.keys())}")
                    return data
                else:
                    self.logger.warning(f"❌ Type inattendu pour {asset}: {type(data)}")
            else:
                self.logger.info(f"❌ {asset} pas dans market_data ou market_data n'est pas un dict")
        
        # Source 3: asset directement dans context
        if asset in context:
            data = context[asset]
            self.logger.info(f"✅ TROUVÉ directement dans context!")
            self.logger.info(f"📄 Type: {type(data)}")
            return data if isinstance(data, dict) else {"raw_data": data}
        
        # Source 4: Variations de casse
        for variant in [asset.lower(), asset.upper()]:
            if variant in context:
                data = context[variant]
                self.logger.info(f"✅ TROUVÉ comme {variant}!")
                return data if isinstance(data, dict) else {"raw_data": data}
        
        # Debug final : lister TOUT le contenu pour comprendre
        self.logger.error(f"❌ ÉCHEC TOTAL pour {asset}")
        self.logger.error("🔍 ANALYSE COMPLÈTE DU CONTEXTE:")
        
        for key, value in context.items():
            if isinstance(value, dict):
                sub_keys = list(value.keys())[:5]  # Premiers 5 éléments
                self.logger.error(f"  📁 {key} (dict): {sub_keys}{'...' if len(value) > 5 else ''}")
            elif isinstance(value, pd.DataFrame):
                self.logger.error(f"  📊 {key} (DataFrame): shape={value.shape}")
            else:
                self.logger.error(f"  📄 {key}: {type(value)}")
        
        return None

    def _build_report_prompt(self, aggregated_data: Dict[str, Any], context: Dict[str, Any]) -> str:
        """
        Construit un prompt pour l'IA afin de générer un rapport journalier basé sur données agrégées.
        Utilise des templates de config pour flexibilité, avec gestion des tokens pour éviter dépassements.
        
        Args:
            aggregated_data (Dict[str, Any]): Les données agrégées journalières (ex. trades, phases, metrics).
            context (Dict): Le contexte système et marché global (enrichi).

        Returns:
            str: Le prompt formaté, prêt à être envoyé à l'IA pour génération de rapport.
        """
        self.logger.info("Construction du prompt pour l'IA via le moteur de templates pour rapport journalier...")

        if not aggregated_data:
            self.logger.warning("Impossible de construire le prompt : les données agrégées sont vides.")
            return ""

        # ZÉRO HARD CODING : Tous les textes sont chargés depuis la configuration.
        role_def = self.config_manager.get(
            "prompts.advisor_report.role_definition",
            "Vous êtes un auditeur IA consultatif pour un bot trading. "
        )
        context_tpl = self.config_manager.get(
            "prompts.advisor_report.global_context_template",
            "\n## Contexte Global\n- Régime: {market_regime}\n- VIX: {vix}\n"
        )
        task_def = self.config_manager.get(
            "prompts.advisor_report.task_definition",
            "\n## Votre Tâche\nFournissez un 'diagnostic' global et des 'suggestions' pour améliorer configs/stratégies (ex. pertinence_strategique par actif). Renvoie en JSON structuré."
        )

        prompt_parts = [role_def]
        prompt_parts.append(
            context_tpl.format(
                market_regime=context.get("current_market_regime", "inconnu"),
                vix=context.get("vix_index", "N/A"),
            )
        )
        prompt_parts.append("## Analyse des Données Journalières\n")

        max_tokens = self.config_manager.get("ai.generation_params.max_tokens", 4096)
        ideal_tokens_ratio = self.config_manager.get("ai.generation_params.ideal_tokens_ratio", 0.75)
        ideal_tokens = int(max_tokens * ideal_tokens_ratio)

        # Adaptation pour données agrégées au lieu d'opportunités par asset
        data_info = []
        for key, value in aggregated_data.items():
            if isinstance(value, list) and value:  # Ex. phases_detected
                data_info.append(f"- {key}: {', '.join(map(str, value[:10]))} (premiers 10 éléments)")
            elif isinstance(value, dict):  # Ex. trades par asset
                for sub_key, sub_value in value.items():
                    data_info.append(f"- {key}.{sub_key}: {sub_value}")
            else:
                data_info.append(f"- {key}: {value}")

        data_block = "\n".join(data_info)

        # Vérification des tokens avant ajout
        current_prompt_estimate = self._estimate_tokens("\n".join(prompt_parts) + "\n" + data_block)
        if current_prompt_estimate > ideal_tokens:
            self.logger.warning(f"Limite de tokens atteinte ({current_prompt_estimate}/{ideal_tokens}), données tronquées.")
            # Tronquer intelligemment : garder les metrics clés
            data_info = data_info[:len(data_info)//2]  # Exemple simple de troncature
            data_block = "\n".join(data_info)

        prompt_parts.append(data_block)

        prompt_parts.append(task_def)
        final_prompt = "\n".join(prompt_parts)

        # Vérification finale
        if self._estimate_tokens(final_prompt) > max_tokens:
            self.logger.error(f"Le prompt final dépasse la limite de tokens ({max_tokens}). L'analyse IA pourrait être incomplète.")
            final_prompt = final_prompt[:max_tokens]  # Tronquer à la limite absolue si dépasse

        self.logger.debug(f"Prompt IA généré (longueur: {len(final_prompt)} caractères).")
        return final_prompt

    def request_ia_advice(self, opportunities: List[str], context: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """
        Délègue la demande de conseil à l'instance du module AIDecision.
        Cette fonction agit comme une interface propre : elle construit le prompt
        et passe la requête au module expert, respectant ainsi le principe de
        responsabilité unique. Implémente un cache pour les réponses IA.

        Args:
            opportunities (List[str]): La shortlist des symboles d'actifs.
            context (Dict): Le contexte système et marché actuel.

        Returns:
            Optional[Dict]: La réponse structurée de l'IA, ou None en cas d'échec.
        """
        self.logger.info(f"Demande de conseil à l'IA pour les opportunités : {opportunities}")

        # AJOUT DEBUG : Examiner la structure du contexte
        self.logger.info(f"🔍 DEBUG AI - Opportunities à analyser: {opportunities}")
        
        market_data = context.get("market_data", {})
        self.logger.info(f"🔍 DEBUG AI - Assets dans market_data: {list(market_data.keys())}")
        
        for asset in opportunities:
            if asset in market_data:
                asset_data = market_data[asset]
                self.logger.info(f"🔍 DEBUG AI - {asset} structure:")
                self.logger.info(f"  Keys disponibles: {list(asset_data.keys())}")
                
                # Chercher les données OHLCV
                possible_data_keys = ["annotated_rates_df", "ohlcv_data", "data", "bars", "candles", "df", "rates"]
                for key in possible_data_keys:
                    if key in asset_data:
                        data = asset_data[key]
                        if hasattr(data, '__len__'):
                            self.logger.info(f"  ✅ Trouvé {key}: {len(data)} éléments, type: {type(data)}")
                            # Si c'est un DataFrame, montrer les colonnes
                            if hasattr(data, 'columns'):
                                self.logger.info(f"    Colonnes DataFrame: {list(data.columns)}")
                        else:
                            self.logger.info(f"  ✅ Trouvé {key}: type {type(data)}")
                    else:
                        self.logger.debug(f"  ❌ Pas de clé {key}")
            else:
                self.logger.error(f"🔍 DEBUG AI - {asset} ABSENT du market_data")
        
        # Vérifier aussi les trading_signals
        trading_signals = context.get("trading_signals", {})
        self.logger.info(f"🔍 DEBUG AI - Assets dans trading_signals: {list(trading_signals.keys())}")
        # FIN DEBUG

        prompt = self.build_ia_prompt(opportunities, context)
        if not prompt:
            self.logger.warning("Prompt IA vide, aucune demande de conseil ne sera envoyée.")
            return {"error": "Prompt IA vide.", "ai_vote_for_configs": {}, "analysis_quality_score": 0.5}

        # S'assurer que l'instance AIDecision est injectée.
        if not self.ai_decision_instance:
            self.logger.error("Interaction IA impossible : l'instance de AIDecision n'a pas été injectée dans AIInterface. Retourne une réponse d'erreur.")
            return {"error": "Module AI non configuré.", "ai_vote_for_configs": {}, "analysis_quality_score": 0.5}

        # Implémenter un cache avec une durée de vie (TTL) pour les réponses de l'IA
        cache_key_elements = {
            "prompt_hash": hashlib.sha256(prompt.encode("utf-8")).hexdigest(),
            "market_regime": context.get("current_market_regime"),
            "vix_index": context.get("vix_index"),
        }
        cache_key = json.dumps(cache_key_elements, sort_keys=True)

        # Récupérer les paramètres du cache IA
        ia_cache_enabled = self.config_manager.get("ai.cache_settings.enabled", False)
        ia_cache_ttl_seconds = self.config_manager.get("ai.cache_settings.ttl_seconds", 300)  # 5 minutes par défaut

        if ia_cache_enabled and cache_key in self._ai_advice_cache:
            cached_advice, timestamp = self._ai_advice_cache[cache_key]
            if (datetime.now(UTC) - timestamp).total_seconds() < ia_cache_ttl_seconds:
                self.logger.info(f"Conseil IA récupéré du cache pour le prompt. (Cache HIT, {ia_cache_ttl_seconds - (datetime.now(UTC) - timestamp).total_seconds():.0f}s restants)")
                return cached_advice
            else:
                self.logger.info("Conseil IA dans le cache expiré. Recalcul.")
                del self._ai_advice_cache[cache_key]  # Supprimer l'entrée expirée

        try:
            # Délégation de l'appel au module expert AIDecision
            advice = self.ai_decision_instance.get_structured_analysis_from_prompt(prompt)
            self.logger.info("Analyse de l'IA reçue avec succès.")

            ai_vote_for_configs = {}
            if advice.get("asset_analysis"):
                for asset_entry in advice["asset_analysis"]:
                    asset_symbol = asset_entry.get("asset")
                    strategic_relevance = asset_entry.get("pertinence_strategique", "Faible")
                    score = {"Élevée": 0.9, "Moyenne": 0.6}.get(strategic_relevance, 0.3)
                    if asset_symbol:
                        ai_vote_for_configs[asset_symbol] = score

            analysis_quality_score = advice.get("analysis_quality_score", 0.5)

            structured_advice = {
                "ai_vote_for_configs": ai_vote_for_configs,
                "analysis_quality_score": analysis_quality_score,
                "summary": advice.get("summary", "Analyse IA"),
                "recommendations": advice.get("recommendations", []),
                "raw_analysis": advice,  # Garder l'analyse brute pour le débogage et l'audit
            }

            # Stocker la réponse dans le cache si activé
            if ia_cache_enabled:
                self._ai_advice_cache[cache_key] = (structured_advice, datetime.now(UTC))
                self.logger.debug("Analyse IA stockée dans le cache.")

            return structured_advice
        except Exception as e:
            self.logger.error(f"Échec de la requête d'analyse à l'IA : {e}", exc_info=True)
            if self.config_manager:
                self.config_manager.send_alert(
                    "ALERTE",
                    f"AI Analyse Échec: {e}",
                    alert_type="telegram_critical",
                )
            return {"error": f"Échec de la requête d'analyse à l'IA: {e}", "ai_vote_for_configs": {}, "analysis_quality_score": 0.5}

    def generate_daily_report(self, aggregated_data: Dict[str, Any], context: Dict[str, Any]) -> Dict[str, Any]:
        """
        Génère un rapport journalier basé sur données agrégées via l'IA consultative.
        Utilise un prompt dédié et parse la réponse en JSON structuré.

        Args:
            aggregated_data (Dict[str, Any]): Données agrégées journalières.
            context (Dict): Contexte système et marché.

        Returns:
            Dict[str, Any]: Rapport structuré ou erreur.
        """
        self.logger.info("Génération du rapport journalier via IA...")

        prompt = self._build_report_prompt(aggregated_data, context)
        if not prompt:
            self.logger.warning("Prompt rapport vide ; rapport non généré.")
            return {"error": "Prompt vide.", "summary": "", "suggestions": [], "performance_score": 0.0}

        if not self.ai_decision_instance:
            self.logger.error("Module AIDecision non injecté ; rapport non généré.")
            return {"error": "Module AI non configuré.", "summary": "", "suggestions": [], "performance_score": 0.0}

        # Cache pour rapports (similaire à advice)
        cache_key_elements = {
            "prompt_hash": hashlib.sha256(prompt.encode("utf-8")).hexdigest(),
            "date": datetime.now().strftime("%Y-%m-%d"),
        }
        cache_key = json.dumps(cache_key_elements, sort_keys=True)

        ia_cache_enabled = self.config_manager.get("ai.cache_settings.enabled", False)
        ia_cache_ttl_seconds = self.config_manager.get("ai.cache_settings.ttl_seconds", 300)

        if ia_cache_enabled and cache_key in self._ai_advice_cache:
            cached_report, timestamp = self._ai_advice_cache[cache_key]
            if (datetime.now(UTC) - timestamp).total_seconds() < ia_cache_ttl_seconds:
                self.logger.info("Rapport IA récupéré du cache.")
                return cached_report
            else:
                del self._ai_advice_cache[cache_key]

        try:
            ai_response = self.ai_decision_instance.get_structured_analysis_from_prompt(prompt)
            report = self._parse_report_response(ai_response)

            if ia_cache_enabled:
                self._ai_advice_cache[cache_key] = (report, datetime.now(UTC))
                self.logger.debug("Rapport IA stocké dans le cache.")

            return report
        except Exception as e:
            self.logger.error(f"Échec génération rapport IA : {e}", exc_info=True)
            if self.config_manager:
                self.config_manager.send_alert("ALERTE", f"Rapport IA Échec: {e}", alert_type="telegram_critical")
            return {"error": str(e), "summary": "", "suggestions": [], "performance_score": 0.0}

    def _parse_report_response(self, response: str) -> Dict[str, Any]:
        """
        Parse la réponse IA en dict JSON pour rapport (avec robustesse).
        """
        try:
            return json.loads(response)
        except json.JSONDecodeError as e:
            self.logger.error(f"Erreur parsing réponse rapport IA : {e}. Retour basique.")
            return {"summary": response, "suggestions": [], "performance_score": 0.0}

    def _estimate_tokens(self, text: str) -> int:
        """
        Estime le nombre de tokens dans un texte en utilisant `tiktoken` si disponible.
        Permet de spécifier l'encodage de `tiktoken` via la configuration.
        Déplacée de ConfigManager.
        """
        tiktoken_encoding_name = self.config_manager.get(
            "ai.token_estimation_encoding", "cl100k_base"
        )  # Nouvelle clé pour l'encodage

        if TIKTOKEN_AVAILABLE:
            try:
                encoding = tiktoken.get_encoding(tiktoken_encoding_name)
                return len(encoding.encode(text))
            except Exception as e:
                self.logger.error(
                    f"Erreur lors de l'estimation des tokens avec tiktoken (encoding: {tiktoken_encoding_name}): {e}. Fallback sur l'heuristique.",
                    exc_info=True,
                )
        else:
            if not hasattr(self, "_warned_about_tokenizer"):
                self.logger.warning(
                    "La bibliothèque 'tiktoken' n'est pas installée. L'estimation du nombre de tokens sera approximative. Installez-la (`pip install tiktoken`) pour une meilleure précision."
                )
                self._warned_about_tokenizer = True  # N'afficher l'avertissement qu'une seule fois.

        token_estimation_ratio = self.config_manager.get("ai.token_estimation_ratio", 4)
        return len(text) // token_estimation_ratio