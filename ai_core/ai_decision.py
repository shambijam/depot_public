# Contenu du fichier ai_core/ai_decision.py

import logging
import os
import json
import sys
import hashlib
import uuid
import numpy as np
import pandas as pd
import time
import requests
from typing import Dict, Any, List, Optional
from datetime import datetime, timezone, UTC
from pathlib import Path
from typing import Dict, Any, List, Optional, Tuple
from llama_cpp import Llama
from core.config_manager import ConfigManager, CustomJSONEncoder
from strategy.base_strategy import BaseStrategy
from tempfile import NamedTemporaryFile

# Initialisation du Logger pour ce module
logger = logging.getLogger(__name__)

# --- TODO: Définir des NamedTuple ou des Pydantic Models pour les structures de données clés ---
# Ceci améliore la lisibilité et la validation des données qui transitent.
# Exemples:
# class AISuggestion(NamedTuple):
#     id: str
#     timestamp: str
#     suggestion_details: Dict[str, Any]
#     status: str # "pending", "approved", "rejected", "reminded"
#     last_reminded: Optional[str]
#
# class AIAnalysisResult(NamedTuple):
#     summary: str
#     recommendations: List[str]
#     confidence_score: float
#     # ... autres champs pertinents


class AIDecision:
    """
    Module du Superviseur IA Global pour le bot de trading institutionnel SNIPER_X.

    Cette classe gère le chargement du modèle d'IA, génère des suggestions de trading
    et d'amélioration du projet, audite le code et l'architecture,
    fournit un chat interactif, gère les notifications et facilite l'apprentissage
    et l'adaptation, toujours sous le contrôle humain.
    """

    # Attributs de classe pour le pattern Singleton
    _instance: Optional["AIDecision"] = None
    _model: Optional[Llama] = None  # Instance du modèle Llama chargé
    _initialized_instance: bool = False  # Pour contrôler l'initialisation du singleton

    def __new__(cls, *args, **kwargs):
        """
        Assure qu'une seule instance d'AIDecision est créée (pattern Singleton).
        Ceci est crucial pour gérer le modèle AI et les ressources associées de manière unique.
        """
        if cls._instance is None:
            cls._instance = super(AIDecision, cls).__new__(cls)
        return cls._instance

    def __init__(
        self,
        model_path: Optional[str] = None,
        config_manager_instance: Optional[ConfigManager] = None,
    ):
        """
        Initialise AIDecision: modèle, prompts, chemins de logs, et historique.

        - Respecte la priorité du paramètre `model_path` sur la config.
        - Normalise les chemins pour éviter 'logs/logs/...' (on garde uniquement le nom de fichier
        pour les fichiers de log et on crée le dossier de logs si nécessaire).
        - Tolérant à l'absence de ConfigManager (fallbacks sûrs).
        - Singleton-friendly: protège la double initialisation.
        """
        from pathlib import Path

        # Empêche l'accès à un attribut possiblement absent
        if not getattr(self, "_initialized_instance", False):
            self._initialized_instance = True

            self.logger = logging.getLogger(__name__)
            self.config_manager = config_manager_instance

            # Petit helper local pour ne garder que le nom de fichier (évite logs/logs/*.jsonl)
            def _filename_only(p: str, default_name: str) -> str:
                try:
                    name = Path(str(p)).name
                    return name if name else default_name
                except Exception:
                    return default_name

            # Cache de conseils IA (clé -> (payload, timestamp))
            self._ai_advice_cache: Dict[str, Tuple[Dict[str, Any], datetime]] = {}

            # ====== Fallback complet si pas de ConfigManager ======
            if self.config_manager is None:
                self.logger.warning(
                    "AIDecision initialisé sans ConfigManager. Fallbacks statiques activés."
                )

                # Modèle
                self.model_path = (
                    model_path
                    if model_path is not None
                    else "./models/llama-2-7b-chat.Q4_K_M.gguf"
                )

                # Prompts / historique
                self.prompts_base_dir = "config/prompts/"
                self.prompts_file_name = "prompts.yaml"
                self.history_path = "logs/suggestion_history.jsonl"

                # Paramètres superviseur IA / rappels
                self.default_initial_suggestion_status = "pending"
                self.min_priority_reminder = 0.7
                self.remind_after_hours = 24
                self.reminder_multiplier = 0.5

                # Logs IA (normalisés)
                self.log_dir = "logs"
                self.ai_supervisor_logs_file = _filename_only(
                    "ai_supervisor_logs.jsonl", "ai_supervisor_logs.jsonl"
                )
                self.ai_supervisor_feedback_file = _filename_only(
                    "ai_supervisor_feedback.jsonl", "ai_supervisor_feedback.jsonl"
                )

                # Templates de message par défaut
                self.message_templates = {
                    "new_suggestion": "💡 Nouvelle Suggestion: {type} - {summary} | Priorité: {priority:.2f}",
                    "pending_suggestion_reminder": "⏰ Rappel: Suggestion en attente - {summary} ({age} jours). Priorité: {priority:.2f}",
                    "compliance_alert": "🚨 Alerte Conformité: {issue} | Item: {item} | Priorité: {priority:.2f} | Action: {action}",
                    "trade_confirmed": "🚀 Trade Confirmé (IA): Symbole: {symbol} | Action: {action} | Volume: {volume} lots",
                    "trade_closed": "📊 Trade Clôturé (IA): Symbole: {symbol} | P&L: ${pnl:.2f} ({status})",
                }

            # ====== Chemin via ConfigManager ======
            else:
                # Modèle
                base_models_dir = self.config_manager.get("paths.models", "models/")
                configured_model_name = self.config_manager.get(
                    "ai.model_name", "llama-2-7b-chat.Q4_K_M.gguf"
                )
                self.model_path = (
                    model_path
                    if model_path is not None
                    else str(Path(base_models_dir) / configured_model_name)
                )

                # Prompts / historique
                self.prompts_base_dir = self.config_manager.get(
                    "paths.prompts", "config/prompts/"
                )
                self.prompts_file_name = self.config_manager.get(
                    "ai.prompt_settings.prompts_file_name", "prompts.yaml"
                )

                # (On laisse history_path tel quel: peut être chemin complet configuré)
                self.history_path = self.config_manager.get(
                    "paths.ai_history_log", "logs/suggestion_history.jsonl"
                )

                # Paramètres superviseur IA / rappels
                self.default_initial_suggestion_status = self.config_manager.get(
                    "ai.suggestion_settings.default_initial_status", "pending"
                )
                self.min_priority_reminder = self.config_manager.get(
                    "ai.supervisor_settings.min_priority", 0.7
                )
                self.remind_after_hours = self.config_manager.get(
                    "ai.supervisor_settings.remind_after_hours", 24
                )
                self.reminder_multiplier = self.config_manager.get(
                    "ai.supervisor_settings.reminder_multiplier", 0.5
                )

                # Logs IA (normalisés)
                self.log_dir = self.config_manager.get("paths.logs", "logs/")
                # Si la config fournit déjà "logs/xxx.jsonl", on garde seulement le nom pour éviter "logs/logs/xxx"
                self.ai_supervisor_logs_file = _filename_only(
                    self.config_manager.get(
                        "paths.ai_supervisor_logs_file", "ai_supervisor_logs.jsonl"
                    ),
                    "ai_supervisor_logs.jsonl",
                )
                self.ai_supervisor_feedback_file = _filename_only(
                    self.config_manager.get(
                        "paths.ai_supervisor_feedback_file",
                        "ai_supervisor_feedback.jsonl",
                    ),
                    "ai_supervisor_feedback.jsonl",
                )

                # Templates de messages (fallback si vide)
                self.message_templates = self.config_manager.get(
                    "telegram.templates", {}
                ) or {
                    "new_suggestion": "💡 Nouvelle Suggestion: {type} - {summary} | Priorité: {priority:.2f}",
                    "pending_suggestion_reminder": "⏰ Rappel: Suggestion en attente - {summary} ({age} jours). Priorité: {priority:.2f}",
                    "compliance_alert": "🚨 Alerte Conformité: {issue} | Item: {item} | Priorité: {priority:.2f} | Action: {action}",
                    "trade_confirmed": "🚀 Trade Confirmé (IA): Symbole: {symbol} | Action: {action} | Volume: {volume} lots",
                    "trade_closed": "📊 Trade Clôturé (IA): Symbole: {symbol} | P&L: ${pnl:.2f} ({status})",
                }

            # Crée le dossier de logs (et sous-dossiers si besoin)
            try:
                Path(self.log_dir).mkdir(parents=True, exist_ok=True)
            except Exception as e:
                self.logger.warning(
                    f"Impossible de créer le dossier de logs '{self.log_dir}': {e}"
                )

            # Éléments d'état
            AIDecision._model = getattr(AIDecision, "_model", None)  # class-level store
            self.prompts: Dict[str, Any] = {}
            self.suggestion_history: List[Dict[str, Any]] = []

            # Chargements initiaux (protégés par try/except à l'intérieur de ces méthodes idéalement)
            self.load_model()
            self._load_prompts()
            self._load_suggestion_history()

            self.logger.info(f"AIDecision initialisé. Modèle: '{self.model_path}'.")

    def _load_prompts(self) -> None:
        """
        Charge les templates de prompts pour l'IA depuis le fichier YAML spécifié
        dans la configuration (`prompts_base_dir` / `prompts_file_name`).
        En cas d'échec de chargement, les fonctions de l'IA basées sur les prompts ne fonctionneront pas.
        """
        try:
            import yaml  # Import local de yaml (nécessaire si non globalement importé)

            # Construire le chemin complet du fichier de prompts
            full_prompts_path = Path(self.prompts_base_dir) / self.prompts_file_name
            with open(full_prompts_path, "r", encoding="utf-8") as f:
                self.prompts = yaml.safe_load(f)
            self.logger.info(
                f"AIDecision: Prompts chargés avec succès depuis '{full_prompts_path}'."
            )  # Utilise self.logger
        except FileNotFoundError:
            self.logger.critical(
                f"AIDecision: Fichier de prompts '{full_prompts_path}' non trouvé. Les fonctions IA basées sur les prompts ne fonctionneront pas. Le bot ne peut pas démarrer en toute sécurité."
            )  # Utilise self.logger
            self.prompts = {}
            # Envoyer une alerte critique si l'absence de prompts est bloquante pour le bot
            self.config_manager.send_alert(
                "CRITIQUE",
                f"AI Prompts Manquants: {full_prompts_path}",
                "telegram_critical",
            )
            raise  # Relance l'exception car c'est critique
        except Exception as e:
            self.logger.critical(
                f"AIDecision: Erreur lors du chargement de '{full_prompts_path}': {e}. Les fonctions IA basées sur les prompts ne fonctionneront pas.",
                exc_info=True,
            )  # Utilise self.logger
            self.prompts = {}
            # Envoyer une alerte critique si l'erreur de chargement est bloquante
            self.config_manager.send_alert(
                "CRITIQUE",
                f"AI Prompts Erreur Chargement: {e}",
                "telegram_critical",
            )
            raise  # Relance l'exception car c'est critique

    def _load_suggestion_history(self) -> None:
        """
        Charge l'historique des suggestions AI depuis un fichier JSONL au démarrage du module.
        Le chemin du fichier est lu dynamiquement depuis la configuration.
        """
        try:
            # Assurez-vous que le répertoire des logs existe avant de tenter de lire
            log_dir = Path(self.log_dir)  # self.log_dir est déjà dynamisé dans __init__
            log_dir.mkdir(parents=True, exist_ok=True)

            if Path(
                self.history_path
            ).exists():  # Utiliser Path pour vérifier l'existence
                with open(self.history_path, "r", encoding="utf-8") as f:
                    for line in f:
                        if line.strip():  # S'assurer que la ligne n'est pas vide
                            self.suggestion_history.append(json.loads(line))
                self.logger.info(
                    f"AIDecision: {len(self.suggestion_history)} suggestions chargées depuis l'historique '{self.history_path}'."
                )  # Utilise self.logger
        except Exception as e:
            self.logger.error(
                f"AIDecision: Impossible de charger l'historique des suggestions depuis '{self.history_path}': {e}",
                exc_info=True,
            )  # Utilise self.logger
            self.suggestion_history = (
                []
            )  # S'assurer que la liste est vide en cas d'erreur
            # Envoyer une alerte si l'échec du chargement de l'historique est critique pour l'opération AI.
            self.config_manager.send_alert(
                "CRITIQUE",
                f"AI Historique Chargement Échec: {e}",
                "telegram_critical",
            )

    def _save_suggestion_history(self) -> None:
        """
        Sauvegarde l'historique actuel des suggestions AI dans un fichier JSONL de manière atomique.
        Le chemin du fichier est lu dynamiquement depuis la configuration.
        """
        try:
            # S'assurer que le répertoire existe
            history_dir = Path(self.history_path).parent
            history_dir.mkdir(parents=True, exist_ok=True)

            # Utilisation de l'écriture atomique : écrire dans un fichier temporaire puis remplacer
            temp_history_path = Path(self.history_path).with_suffix(".tmp")

            with open(temp_history_path, "w", encoding="utf-8") as f:
                # Utiliser CustomJSONEncoder pour gérer les types de données non sérialisables (datetime, UUID, etc.)
                # ConfigManager fournit CustomJSONEncoder, s'assurer qu'il est accessible ici
                from core.config_manager import (
                    CustomJSONEncoder,
                )  # Import local pour cette fonction

                for suggestion in self.suggestion_history:
                    f.write(json.dumps(suggestion, cls=CustomJSONEncoder) + "\n")

            # CORRECTION: Utiliser Path.replace() au lieu de Path.rename() pour gérer l'écrasement sur Windows.
            # Path.replace() est atomique et gère le cas où le fichier de destination existe déjà.
            temp_history_path.replace(self.history_path)

            self.logger.info(
                f"AIDecision: Historique de {len(self.suggestion_history)} suggestions sauvegardé avec succès dans '{self.history_path}'."
            )  # Utilise self.logger
        except Exception as e:
            self.logger.error(
                f"AIDecision: Échec de la sauvegarde de l'historique des suggestions vers '{self.history_path}': {e}",
                exc_info=True,
            )  # Utilise self.logger
            # Envoyer une alerte critique si la sauvegarde de l'historique échoue.
            if temp_history_path.exists():
                temp_history_path.unlink()  # Nettoyer le fichier temporaire en cas d'erreur
            if self.config_manager:
                # CORRECTION: Passage de l'argument alert_type en positionnel
                self.config_manager.send_alert(
                    f"AI Historique Sauvegarde Échec: {e}", "telegram_critical"
                )

    def load_model(self) -> None:
        """
        Charge le modèle d'IA Llama (GGUF) en utilisant la librairie `llama-cpp-python`.
        Les paramètres de chargement du modèle (n_gpu_layers, n_ctx) sont lus dynamiquement.
        Gère les erreurs de chargement de manière robuste.
        """
        if (
            AIDecision._model is not None
        ):  # Vérifier si le modèle de classe est déjà chargé
            self.model = AIDecision._model
            self.logger.info(
                "AIDecision: Modèle Llama déjà chargé, réutilisation de l'instance existante."
            )  # Utilise self.logger
            return

        self.logger.info(
            f"AIDecision: Tentative de chargement du modèle Llama depuis: '{self.model_path}'..."
        )  # Utilise self.logger

        # Paramètres de chargement du modèle Llama (n_gpu_layers, n_ctx)
        n_gpu_layers = (
            -1
        )  # Valeur par défaut pour déléguer à tous les GPU si disponibles
        n_ctx = 4096  # Valeur par défaut pour la taille du contexte
        main_gpu_device = 0  # Par défaut, utilise la carte 0 pour le main_gpu_device
        rope_freq_base = 10000  # Paramètre Llama.cpp pour les fréquences RoPE
        rope_freq_scale = 1.0  # Paramètre Llama.cpp pour le scaling RoPE

        if self.config_manager:
            ai_config = self.config_manager.get_current_dynamic_config().get("ai", {})
            generation_params = ai_config.get("generation_params", {})
            n_gpu_layers = generation_params.get("n_gpu_layers", n_gpu_layers)
            n_ctx = generation_params.get("n_ctx", n_ctx)
            main_gpu_device = generation_params.get(
                "main_gpu_device", main_gpu_device
            )  # Nouveau
            rope_freq_base = generation_params.get(
                "rope_freq_base", rope_freq_base
            )  # Nouveau
            rope_freq_scale = generation_params.get(
                "rope_freq_scale", rope_freq_scale
            )  # Nouveau

            self.logger.info(
                f"AIDecision: Paramètres de chargement Llama récupérés de la config: n_gpu_layers={n_gpu_layers}, n_ctx={n_ctx}, main_gpu_device={main_gpu_device}."
            )  # Utilise self.logger
        else:
            self.logger.warning(
                "AIDecision: ConfigManager non disponible pour charger les paramètres Llama. Utilisation des valeurs par défaut pour le chargement du modèle."
            )  # Utilise self.logger

        try:
            # Vérifier si le fichier du modèle existe avant de tenter le chargement
            if not Path(self.model_path).is_file():
                raise FileNotFoundError(
                    f"Fichier du modèle AI introuvable: {self.model_path}. Veuillez vérifier le chemin ou télécharger le modèle."
                )

            # Charger le modèle en utilisant les paramètres dynamiques
            AIDecision._model = Llama(
                model_path=self.model_path,
                n_gpu_layers=n_gpu_layers,
                n_ctx=n_ctx,
                main_gpu=main_gpu_device,  # Utilise main_gpu_device
                rope_freq_base=rope_freq_base,  # Utilise rope_freq_base
                rope_freq_scale=rope_freq_scale,  # Utilise rope_freq_scale
                verbose=False,  # Rendre moins verbeux le chargement
            )
            self.model = AIDecision._model  # Assigner à l'instance locale
            self.logger.info(
                f"AIDecision: Modèle Llama depuis '{self.model_path}' chargé avec succès."
            )  # Utilise self.logger
        except FileNotFoundError as fnfe:
            self.logger.critical(
                f"AIDecision: FATAL: Le fichier du modèle Llama n'a pas été trouvé. Erreur: {fnfe}. Le bot ne peut pas fonctionner sans modèle AI.",
                exc_info=True,
            )  # Utilise self.logger
            self.model = None
            AIDecision._model = None
            if self.config_manager:
                self.config_manager.send_alert(
                    "CRITIQUE",
                    f"AI Modèle Manquant: {self.model_path}",
                    "telegram_critical",
                )
            raise  # Important de relancer l'exception car le bot ne peut pas fonctionner sans modèle AI
        except Exception as e:
            self.logger.critical(
                f"AIDecision: FATAL: Échec du chargement du modèle Llama depuis '{self.model_path}': {e}. Le bot ne peut pas fonctionner sans modèle AI.",
                exc_info=True,
            )  # Utilise self.logger
            self.model = None
            AIDecision._model = None  # S'assurer que le modèle de classe est aussi None
            if self.config_manager:
                self.config_manager.send_alert(
                    "CRITIQUE",
                    f"AI Modèle Chargement Échec: {e}",
                    "telegram_critical",
                )
            raise  # Important de relancer l'exception car le bot ne peut pas fonctionner sans modèle AI

    def _generate_raw_response(
        self, prompt: str, max_tokens: Optional[int] = None
    ) -> str:  # max_tokens devient optionnel
        """
        Génère une réponse brute à partir du modèle AI Llama chargé.
        Les paramètres de génération (max_tokens, temperature, top_p, stop_sequences)
        sont lus dynamiquement depuis la configuration.

        Args:
            prompt (str): Le prompt textuel à envoyer au modèle AI.
            max_tokens (int, optional): Le nombre maximal de tokens à générer.
                                       Si `None`, la valeur configurée sera utilisée.

        Returns:
            str: La réponse brute générée par le modèle AI.
                 Retourne une chaîne JSON encodée avec un message d'erreur en cas d'échec.
        """
        if not self.model:  # Utilise self.model qui est un attribut d'instance
            self.logger.error(
                "AIDecision: Modèle AI Llama non chargé. Impossible de générer une réponse."
            )  # Utilise self.logger
            return json.dumps({"error": "AI model not loaded"})

        # Paramètres de génération de l'IA (max_tokens, temperature, top_p, stop_sequences)
        # Ces valeurs sont lues depuis la section 'ai.generation_params' de la configuration.
        current_max_tokens = (
            max_tokens
            if max_tokens is not None
            else self.config_manager.get("ai.generation_params.max_tokens", 2048)
        )
        temperature = self.config_manager.get("ai.generation_params.temperature", 0.7)
        top_p = self.config_manager.get("ai.generation_params.top_p", 0.9)
        # Assurez-vous que la clé 'stop_sequences' est bien dans votre prod_config.json
        stop_sequences = self.config_manager.get(
            "ai.generation_params.stop_sequences",
            ["User query:", "\n```json", "\n```", "---", "###", "```python"],
        )

        self.logger.debug(
            f"AIDecision: Paramètres de génération AI chargés: max_tokens={current_max_tokens}, temp={temperature}, top_p={top_p}."
        )  # Utilise self.logger

        try:
            response = self.model.create_completion(
                prompt,
                max_tokens=current_max_tokens,  # Utilise la valeur dynamique
                temperature=temperature,  # Utilise la valeur dynamique
                top_p=top_p,  # Utilise la valeur dynamique
                stop=stop_sequences,  # Utilise la valeur dynamique
            )
            return response["choices"][0]["text"].strip()
        except Exception as e:
            self.logger.error(
                f"AIDecision: Erreur lors de la génération de la réponse AI brute avec Llama: {e}",
                exc_info=True,
            )  # Utilise self.logger
            # Envoyer une alerte si la génération AI échoue (non critique, car peut être temporaire)
            if self.config_manager:
                self.config_manager.send_alert(
                    f"AI Génération Réponse Échec: {e}", "telegram_critical"
                )
            return json.dumps({"error": f"Failed to generate response: {e}"})

    def parse_response(self, raw_response: str) -> Dict[str, Any]:
        """
        Tente de parser une réponse JSON brute provenant du modèle AI.
        Cette méthode gère les cas où le JSON est imbriqué dans des blocs de code Markdown
        ou contient du texte supplémentaire, en essayant d'extraire et de valider le JSON.

        Args:
            raw_response (str): La chaîne de caractères brute reçue du modèle AI.

        Returns:
            Dict[str, Any]: Le dictionnaire Python parsé à partir du JSON.
                            Inclut une clé 'error' si le parsing échoue.
        """
        self.logger.debug(
            f"AIDecision: Tentative de parsing de la réponse brute AI. Extrait: {raw_response[:100]}..."
        )  # Utilise self.logger

        # Nettoyer la réponse brute pour isoler le JSON
        json_str = raw_response.strip()
        # Tenter de trouver le JSON à l'intérieur des blocs de code Markdown (```json...```)
        if "```json" in json_str:
            try:
                # Extraire le contenu entre les balises ```json```
                json_str = json_str.split("```json", 1)[1].split("```")[0].strip()
            except IndexError:
                # Si les balises sont mal formées, essayer de parser la chaîne brute
                self.logger.warning(
                    "AIDecision: Balises '```json' trouvées mais format invalide. Tentative de parsing de la chaîne entière."
                )

        try:
            # Tenter de parser la chaîne JSON
            parsed_data = json.loads(json_str)
            self.logger.debug("AIDecision: Parsing JSON réussi.")  # Utilise self.logger
            return parsed_data
        except json.JSONDecodeError as e:
            self.logger.error(
                f"AIDecision: Erreur de parsing JSON: {e}. Réponse brute: {raw_response[:200]}...",
                exc_info=True,
            )  # Utilise self.logger
            # Si le parsing direct échoue, essayer de "sauver" le JSON en recherchant les accolades
            try:
                first_brace = json_str.find("{")
                last_brace = json_str.rfind("}")
                if first_brace != -1 and last_brace != -1 and first_brace < last_brace:
                    salvaged_json_str = json_str[first_brace : last_brace + 1]
                    parsed_data = json.loads(salvaged_json_str)
                    self.logger.warning(
                        f"AIDecision: JSON récupéré avec succès à partir de la réponse brute. Récupéré: {salvaged_json_str[:200]}..."
                    )  # Utilise self.logger
                    return parsed_data
            except Exception as salvage_e:
                self.logger.error(
                    f"AIDecision: Échec de la récupération du JSON: {salvage_e}",
                    exc_info=True,
                )  # Utilise self.logger

            # Envoyer une alerte si le parsing JSON échoue de manière critique pour une décision
            if self.config_manager:
                self.config_manager.send_alert(
                    f"AI: Échec parsing JSON de la réponse. {e}", "telegram_critical"
                )
            return {
                "error": "Échec du parsing de la réponse AI en JSON",
                "raw_response": raw_response,
            }
        except Exception as e:
            self.logger.error(
                f"AIDecision: Erreur inattendue dans parse_response: {e}. Réponse brute: {raw_response[:200]}...",
                exc_info=True,
            )  # Utilise self.logger
            if self.config_manager:
                self.config_manager.send_alert(
                    f"AI: Erreur inattendue parsing réponse: {e}", "telegram_critical"
                )
            return {
                "error": "Erreur inattendue lors du parsing de la réponse",
                "raw_response": raw_response,
            }
            # Ajouter cette méthode dans la classe AIDecision (ai_decision.py)

    def get_structured_analysis_from_prompt(self, prompt: str) -> Dict[str, Any]:
        """
        Génère une analyse structurée à partir d'un prompt donné.
        """
        try:
            self.logger.info(
                "AIDecision: Génération d'analyse structurée depuis prompt..."
            )

            # Générer la réponse brute du modèle
            raw_response = self._generate_raw_response(prompt)

            # 🆕 AJOUT: Vérifier si la réponse est vide
            if not raw_response or raw_response.strip() == "":
                self.logger.warning(
                    "Réponse IA vide, création d'une analyse par défaut"
                )
                return self._create_fallback_analysis("Réponse vide du modèle IA")

            # Parser la réponse en JSON structuré
            parsed_response = self.parse_response(raw_response)

            # Si parsing échoue, créer une structure par défaut
            if "error" in parsed_response:
                self.logger.warning(
                    f"Parsing JSON échoué, création d'une structure par défaut"
                )
                return self._create_fallback_analysis(raw_response)

            # Valider et enrichir la réponse
            validated_response = self._validate_and_enrich_analysis(parsed_response)

            self.logger.info("Analyse structurée générée avec succès")
            return validated_response

        except Exception as e:
            self.logger.error(
                f"Erreur lors de la génération d'analyse structurée : {e}",
                exc_info=True,
            )
            return {
                "error": f"Échec génération analyse : {e}",
                "asset_analysis": [],
                "summary": "Erreur lors de l'analyse IA",
                "recommendations": [],
                "analysis_quality_score": 0.0,
            }

    def _create_fallback_analysis(self, raw_response: str) -> Dict[str, Any]:
        """
        Crée une analyse par défaut quand le parsing JSON échoue.

        Args:
            raw_response (str): La réponse brute du modèle

        Returns:
            Dict[str, Any]: Structure d'analyse par défaut
        """
        # Essayer d'extraire des informations basiques de la réponse textuelle
        lines = raw_response.split("\n")
        summary_line = next(
            (line for line in lines if line.strip()), "Analyse IA disponible"
        )[:200]

        return {
            "asset_analysis": [
                {
                    "asset": "ANALYSE_GENERALE",
                    "pertinence_strategique": "Moyenne",
                    "diagnostic": summary_line,
                }
            ],
            "summary": summary_line,
            "recommendations": ["Vérifier la configuration des prompts IA"],
            "analysis_quality_score": 0.3,  # Score faible car c'est un fallback
            "raw_response": raw_response,
            "parsing_method": "fallback_text_analysis",
        }

    def _validate_and_enrich_analysis(self, analysis: Dict[str, Any]) -> Dict[str, Any]:
        """
        Valide et enrichit l'analyse parsée pour s'assurer qu'elle a tous les champs requis.

        Args:
            analysis (Dict[str, Any]): L'analyse parsée depuis JSON

        Returns:
            Dict[str, Any]: L'analyse validée et enrichie
        """
        # Champs obligatoires avec valeurs par défaut
        required_fields = {
            "asset_analysis": [],
            "summary": "Analyse IA",
            "recommendations": [],
            "analysis_quality_score": 0.5,
        }

        # Ajouter les champs manquants
        for field, default_value in required_fields.items():
            if field not in analysis:
                analysis[field] = default_value

        # Valider asset_analysis
        if not isinstance(analysis["asset_analysis"], list):
            analysis["asset_analysis"] = []

        # S'assurer que chaque actif a les champs requis
        for asset_entry in analysis["asset_analysis"]:
            if isinstance(asset_entry, dict):
                asset_entry.setdefault("asset", "UNKNOWN")
                asset_entry.setdefault("pertinence_strategique", "Faible")
                asset_entry.setdefault("diagnostic", "Analyse en cours")

        # Valider le score de qualité
        if not isinstance(analysis.get("analysis_quality_score"), (int, float)):
            analysis["analysis_quality_score"] = 0.5
        else:
            # Limiter entre 0 et 1
            analysis["analysis_quality_score"] = max(
                0.0, min(1.0, analysis["analysis_quality_score"])
            )

        # Ajouter métadonnées
        analysis["validation_timestamp"] = datetime.now(UTC).isoformat()
        analysis["model_path"] = self.model_path

        return analysis

    # BONUS: Méthode alternative pour compatibilité étendue
    def analyze_prompt(self, prompt: str) -> Dict[str, Any]:
        """
        Alias pour get_structured_analysis_from_prompt pour compatibilité.
        """
        return self.get_structured_analysis_from_prompt(prompt)

    def get_analysis(self, prompt: str) -> Dict[str, Any]:
        """
        Autre alias pour get_structured_analysis_from_prompt pour compatibilité.
        """
        return self.get_structured_analysis_from_prompt(prompt)

    def chat_mode(self) -> None:
        """
        Permet à l'utilisateur d'interagir directement avec le modèle AI en mode conversationnel.
        Le rôle de l'IA et les prompts de conversation sont externalisés dans le fichier de prompts.
        """
        self.logger.info(
            "AIDecision: Entrée en Mode Chat IA. Tapez 'exit' ou 'quit' pour terminer la session."
        )  # Utilise self.logger
        print("\n--- SNIPER_X AI Chat Mode ---")
        print(
            "Posez-moi des questions sur le trading, le code, l'architecture, la stratégie, la conformité ou l'apprentissage !"
        )
        print("Tapez 'exit' ou 'quit' pour terminer.")

        # 1. Récupérer le prompt "système" qui définit le rôle de l'IA
        # Utilise la clé 'chat_mode_system_prompt' du dictionnaire `self.prompts` chargé par `_load_prompts`.
        system_prompt = self.prompts.get(
            "chat_mode_system_prompt",
            "You are a helpful AI assistant tasked with answering questions about trading, coding, and system architecture.",  # Un rôle par défaut plus spécifique si le prompt n'est pas trouvé
        )

        while True:
            user_input = input("\nVous (Consultant AI SNIPER_X): ")
            if user_input.lower() in ["exit", "quit"]:
                self.logger.info(
                    "AIDecision: Quitter le mode chat."
                )  # Utilise self.logger
                print("Fin du mode chat. Au revoir !")
                break

            if not user_input.strip():
                print("Veuillez taper quelque chose.")
                continue

            # 2. Construire le prompt final en combinant le rôle et la question de l'utilisateur
            full_prompt = f"System: {system_prompt}\nUser: {user_input}\nAssistant:"

            # _generate_raw_response gère déjà max_tokens dynamiquement, donc pas besoin ici.
            response = self._generate_raw_response(full_prompt)
            print(f"\nAI (Consultant AI SNIPER_X): {response}")

            # Journaliser l'interaction en mode chat
            self.log_decision(
                {
                    "query": user_input,
                    "response_excerpt": response[:500],
                },  # Limiter l'extrait pour le log
                {"raw_response": response},
                "Chat Mode Interaction",
            )

    def generate_decision(
        self, prompt_key: str, context: Dict[str, Any], strategy: Optional[str] = None
    ) -> Dict[str, Any]:
        """
        Génère une proposition de trading (ou une suggestion générale) en utilisant une clé de prompt pour construire
        dynamiquement la requête à l'IA. Cette fonction est le point d'entrée pour la génération de suggestions par l'IA.

        Args:
            prompt_key (str): La clé du prompt à utiliser depuis les prompts chargés (ex: 'generate_trade_suggestion').
            context (Dict[str, Any]): Le contexte de marché et système actuel à inclure dans le prompt (enrichi par PhaseObserver).
            strategy (Optional[str]): Nom de la stratégie spécifique à laquelle l'IA devrait se concentrer (optionnel).

        Returns:
            Dict[str, Any]: Le dictionnaire de suggestion généré par l'IA. Inclut une clé 'error' en cas d'échec.
        """
        # 1. Récupérer le modèle de prompt depuis le fichier YAML
        prompt_template = self.prompts.get(prompt_key)
        if not prompt_template:
            self.logger.error(
                f"AIDecision: Le prompt '{prompt_key}' est manquant dans le fichier de prompts. Impossible de générer la suggestion."
            )
            if self.config_manager:
                self.config_manager.send_alert(
                    "CRITIQUE",
                    f"AI: Prompt '{prompt_key}' manquant.",
                    "telegram_critical",
                )
            return {"error": f"Prompt '{prompt_key}' non configuré."}

        # 2. Construire le prompt final en formatant avec le contexte et la stratégie
        # Utiliser CustomJSONEncoder pour sérialiser le contexte afin de gérer les DataFrames et autres types complexes
        try:
            context_json = json.dumps(
                context, indent=2, cls=ConfigManager.CustomJSONEncoder
            )
        except Exception as e:
            self.logger.error(
                f"AIDecision: Erreur de sérialisation du contexte pour le prompt IA: {e}. Le prompt sera incomplet.",
                exc_info=True,
            )
            context_json = json.dumps({"error": f"Context serialization failed: {e}"})

        full_prompt = prompt_template.format(context=context_json)
        if strategy:
            full_prompt += f"\nConcentre-toi sur une stratégie de type : {strategy}."

        self.logger.info(
            f"AIDecision: Génération d'une proposition de trading avec le prompt '{prompt_key}'..."
        )
        raw_response = self._generate_raw_response(full_prompt)
        # La réponse de l'IA est maintenant une 'proposition' ou 'analyse', plus une 'décision'
        ai_suggestion = self.parse_response(raw_response)

        # 3. Enrichir et notifier la suggestion (si le parsing est réussi et aucune erreur)
        if "error" not in ai_suggestion:
            # Le score devient un score de confiance de la suggestion, pas de la "décision"
            score = self.score_decision(ai_suggestion, context)
            ai_suggestion["confidence_score"] = score
            ai_suggestion["explanation"] = self.explain_decision(ai_suggestion, context)

            # Ajouter à l'historique des suggestions AI, car ce n'est plus une décision directe mais une "suggestion"
            ai_suggestion["suggestion_type"] = ai_suggestion.get(
                "suggestion_type", "trade_proposal"
            )  # Nouveau type de suggestion
            self._add_suggestion_to_history(
                ai_suggestion
            )  # Ajouter la suggestion à l'historique des suggestions.

            # Journaliser la suggestion (au lieu de "décision")
            self.log_decision(
                ai_suggestion, context, "Trading suggestion generated", score
            )

            # Notification Telegram : utilise le type "new_suggestion" plus générique
            self.notify_telegram(
                "new_suggestion",  # Utilise le type d'événement pour la notification
                {
                    "type": ai_suggestion[
                        "suggestion_type"
                    ],  # Le type est maintenant tiré de la suggestion elle-même
                    "summary": f"Proposition de trade: {ai_suggestion.get('trade_type', 'N/A')} pour {ai_suggestion.get('asset', 'N/A')}",
                    "priority": score,
                    "asset": ai_suggestion.get(
                        "asset"
                    ),  # Passer les infos pour le template
                    "trade_type": ai_suggestion.get("trade_type"),
                },
            )
        return ai_suggestion  # Retourne la suggestion de l'IA

    def audit_trading_performance(
        self,
        logs: List[Dict[str, Any]],
        period: str = "last_day",
        current_context: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """
        Audite la performance de trading du bot en analysant les logs historiques
        pour une période donnée, en utilisant un prompt externalisé.
        Génère un rapport d'audit de performance complet, avec un bilan par actif,
        les raisons des gains/pertes, l'analyse par phase de marché, et des suggestions d'amélioration.
        Ce rapport est sauvegardé dans le dossier /config/ai_audit.
        """
        from pathlib import Path
        import json

        self.logger.info(
            f"AIDecision: Démarrage de l'audit de performance de trading pour la période '{period}'..."
        )

        # --- helpers locaux ---
        def _json_dumps_safe(obj, indent=2):
            """Encodage JSON robuste: tente CustomJSONEncoder, sinon fallback std avec default=str."""
            try:
                CustomJSONEncoder = getattr(self.config_manager, "CustomJSONEncoder", None)
                if CustomJSONEncoder:
                    return json.dumps(obj, indent=indent, cls=CustomJSONEncoder)
            except Exception:
                pass
            try:
                return json.dumps(obj, indent=indent, ensure_ascii=False, default=str)
            except Exception:
                return json.dumps(str(obj), indent=indent, ensure_ascii=False)

        def _safe_alert(msg: str, channel: str = "telegram_critical"):
            """Compat signature send_alert(message, alert_type='telegram_critical')."""
            try:
                if self.config_manager:
                    # priorité aux kwargs (évite l'erreur '4 were given')
                    self.config_manager.send_alert(message=msg, alert_type=channel)
            except TypeError:
                try:
                    # fallback positionnel (2 args attendus: message, alert_type)
                    self.config_manager.send_alert(msg, channel)
                except Exception:
                    pass
            except Exception:
                pass

        prompt_template = self.prompts.get("audit_trading_performance")
        if not prompt_template:
            self.logger.error(
                "AIDecision: Le prompt 'audit_trading_performance' est manquant. Impossible d'auditer la performance."
            )
            _safe_alert("AI Prompt Manquant: audit_trading_performance")
            return {"error": "Prompt 'audit_trading_performance' non configuré."}

        # Taille d'échantillon maximum envoyée à l'IA (sécurité mémoire/coût)
        try:
            log_sample_size = int(self.config_manager.get("ai.supervisor_settings.log_sample_size", 100))
        except Exception:
            log_sample_size = 100

        self.logger.debug(
            f"AIDecision: Utilisation d'une taille d'échantillon de logs de {log_sample_size} pour l'audit de performance."
        )

        # === Agrégations ===
        trading_summary_by_asset: Dict[str, Dict[str, Any]] = {}
        trade_details_for_ai: List[Dict[str, Any]] = []

        for log_entry in logs or []:
            if log_entry.get("event_type") != "ai_feedback":
                continue

            original_decision = log_entry.get("original_decision", {}) or {}
            outcome = log_entry.get("outcome", {}) or {}

            # On retient uniquement les trades exécutés et clôturés avec PnL
            if outcome.get("status") != "executed" or "pnl_usd" not in outcome:
                continue

            asset = original_decision.get("asset", "UNKNOWN_ASSET")
            strategy_type = original_decision.get("strategy_type", "UNKNOWN_STRATEGY")
            pnl_usd = float(outcome.get("pnl_usd", 0.0) or 0.0)

            # Phase de marché au moment du trade (si dispo)
            context_summary = log_entry.get("context_at_gen", {}) or {}
            market_data_summary = (context_summary.get("market_data_summary", {}) or {}).get(asset, {}) or {}
            phase_at_trade = market_data_summary.get("phase", "N/A")

            # Init agrégat asset si besoin
            if asset not in trading_summary_by_asset:
                trading_summary_by_asset[asset] = {
                    "total_trades": 0,
                    "wins": 0,
                    "losses": 0,
                    "total_pnl": 0.0,
                    "pnl_by_phase": {},
                    "win_rate_by_phase": {},
                }

            asset_summary = trading_summary_by_asset[asset]
            asset_summary["total_trades"] += 1
            asset_summary["total_pnl"] += pnl_usd
            if pnl_usd > 0:
                asset_summary["wins"] += 1
            elif pnl_usd < 0:
                asset_summary["losses"] += 1

            # Agrégation par phase
            if phase_at_trade not in asset_summary["pnl_by_phase"]:
                asset_summary["pnl_by_phase"][phase_at_trade] = {"total_pnl": 0.0, "trades": 0, "wins": 0}
            phase_bucket = asset_summary["pnl_by_phase"][phase_at_trade]
            phase_bucket["total_pnl"] += pnl_usd
            phase_bucket["trades"] += 1
            if pnl_usd > 0:
                phase_bucket["wins"] += 1

            # Échantillon détaillé (pour prompt IA)
            trade_details_for_ai.append(
                {
                    "asset": asset,
                    "strategy": strategy_type,
                    "action": original_decision.get("action"),
                    "volume": original_decision.get("volume"),
                    "pnl_usd": pnl_usd,
                    "status": outcome.get("status"),
                    "reason_closure": outcome.get("message", "N/A"),
                    "phase_at_entry": phase_at_trade,
                    "entry_signals": market_data_summary,  # signaux/état au moment de la décision
                }
            )

        # Win-rate par phase (par actif)
        for asset_sum in trading_summary_by_asset.values():
            for phase, data in asset_sum["pnl_by_phase"].items():
                trades_n = max(0, int(data.get("trades", 0) or 0))
                wins_n = max(0, int(data.get("wins", 0) or 0))
                asset_sum["win_rate_by_phase"][phase] = round((wins_n / trades_n) * 100, 2) if trades_n > 0 else 0.0

        # Totaux globaux (pour résumé/notification)
        total_trades = sum(v["total_trades"] for v in trading_summary_by_asset.values()) if trading_summary_by_asset else 0
        total_wins = sum(v["wins"] for v in trading_summary_by_asset.values()) if trading_summary_by_asset else 0
        total_losses = sum(v["losses"] for v in trading_summary_by_asset.values()) if trading_summary_by_asset else 0
        total_pnl = float(sum(v["total_pnl"] for v in trading_summary_by_asset.values())) if trading_summary_by_asset else 0.0
        win_rate = round((total_wins / total_trades) * 100, 2) if total_trades > 0 else 0.0

        # Sérialisations robustes pour le prompt
        truncated_trade_details_json = _json_dumps_safe(trade_details_for_ai[-log_sample_size:]) if trade_details_for_ai else "None"
        trading_summary_json = _json_dumps_safe(trading_summary_by_asset)
        context_json = _json_dumps_safe(current_context) if current_context else "None"

        # Construction du prompt
        try:
            prompt = prompt_template.format(
                period=period,
                trading_summary_by_asset=trading_summary_json,
                trade_details_sample=truncated_trade_details_json,
                context=context_json,
            )
        except Exception as e:
            self.logger.error(
                f"AIDecision: Erreur de sérialisation pour le prompt 'audit_trading_performance': {e}.",
                exc_info=True,
            )
            _safe_alert(f"AI: Erreur sérialisation audit_trading_performance: {e}")
            return {"error": f"Erreur de sérialisation pour le prompt: {e}"}

        # Appel modèle IA
        raw_response = self._generate_raw_response(prompt)
        audit_results = self.parse_response(raw_response)

        if "error" not in audit_results:
            # Métadonnées utiles
            audit_results["suggestion_type"] = "performance_audit_report"
            audit_results["totals"] = {
                "total_trades": total_trades,
                "wins": total_wins,
                "losses": total_losses,
                "total_pnl": total_pnl,
                "win_rate": win_rate,
            }
            self._add_suggestion_to_history(audit_results)

            # Sauvegarde du rapport
            report_content_markdown = self._format_audit_report_for_file(
                audit_results, trading_summary_by_asset, period
            )
            report_file_path = self._save_audit_report_to_file(report_content_markdown, period)
            audit_results["report_file_path"] = str(report_file_path)

            # Notification (résumé propre, avec vrais totaux)
            try:
                report_summary_message = (
                    f"📊 **Rapport d'Audit SNIPER_X — {period}**\n\n"
                    f"Résumé : {audit_results.get('audit_summary', 'Audit complet disponible dans le rapport.')}\n"
                    f"Trades: {total_trades} | Wins: {total_wins} | Losses: {total_losses}\n"
                    f"P&L Total: ${total_pnl:.2f} | Win rate: {win_rate:.2f}%\n"
                    f"Rapport : {Path(report_file_path).name}"
                )
                self.notify_telegram(
                    "daily_audit_report_available",
                    {
                        "report_name": Path(report_file_path).name,
                        "summary": audit_results.get(
                            "audit_summary", f"Rapport d'audit de performance pour {period}"
                        ),
                        "telegram_message": report_summary_message,
                    },
                )
            except Exception as e:
                # On n'empêche pas la réussite de l'audit si la notif échoue
                self.logger.warning(f"AIDecision: Notification Telegram échouée: {e}")

        return audit_results

        
    def generate_daily_ai_reports(
        self,
        ai_decision,            # instance AIDecision (ou équivalent)
        mecano,                 # instance Mecano (ou équivalent)
        config_manager,         # instance ConfigManager (accès .get)
        period_days: int = 1,   # 1 = quotidien ; 7 = hebdo, etc.
    ) -> dict:
        """
        Génère et SAUVE les rapports des deux clients (IA Decision + Mecano) dans le même dossier.
        - Résout le chemin unique depuis la config (paths.ai_audit), sinon fallback "config/ai_audit".
        - Création atomique des fichiers.
        - Robuste aux signatures/méthodes légèrement différentes (hasattr + fallbacks).
        - Retourne les chemins des fichiers créés pour monitoring externe.

        Écrit :
        - Rapport IA (Markdown)  : ai_performance_audit_report_daily_<timestamp>.md
        - Rapport Mecano (JSON)  : mecano_report_daily_<timestamp>.json
        """
                

        # ---------- 1) Résolution dossier sortie unique ----------
        try:
            audit_dir = config_manager.get("paths.ai_audit", None)
            if not audit_dir:
                # certains setups déposent sous configs/
                base_cfg = config_manager.get("paths.configs", "config")
                audit_dir = os.path.join(base_cfg, "ai_audit")
        except Exception:
            audit_dir = os.path.join("config", "ai_audit")

        out_dir = Path(audit_dir)
        out_dir.mkdir(parents=True, exist_ok=True)

        ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")

        # utilitaires d'écriture atomique
        def _atomic_write_text(path: Path, content: str, encoding: str = "utf-8"):
            path.parent.mkdir(parents=True, exist_ok=True)
            with NamedTemporaryFile("w", delete=False, dir=str(path.parent), encoding=encoding) as tmp:
                tmp.write(content)
                tmp_path = Path(tmp.name)
            tmp_path.replace(path)

        def _atomic_write_json(path: Path, payload: dict):
            _atomic_write_text(path, json.dumps(payload, ensure_ascii=False, indent=2))

        results = {
            "ai_decision_report_path": None,
            "mecano_report_path": None,
            "errors": []
        }

        # ---------- 2) Rapport IA (Markdown) ----------
        try:
            # a) Génération du contenu (plusieurs variantes possibles selon l’implémentation)
            md_content = None

            if hasattr(ai_decision, "audit_trading_performance"):
                # idéal : fonction dédiée d’audit (peut accepter une période)
                try:
                    md_content = ai_decision.audit_trading_performance(period_days=period_days)
                except TypeError:
                    md_content = ai_decision.audit_trading_performance()
            elif hasattr(ai_decision, "generate_daily_report"):
                # fallback : génère un dict → on le transforme en markdown simple
                rep = ai_decision.generate_daily_report(period_days=period_days)
                md_lines = ["# AI Daily Report", ""]
                if isinstance(rep, dict):
                    for k, v in rep.items():
                        md_lines.append(f"## {k}\n{v}\n")
                else:
                    md_lines.append(str(rep))
                md_content = "\n".join(md_lines)
            else:
                raise RuntimeError("AIDecision ne fournit pas d’API de génération (audit_trading_performance / generate_daily_report manquantes).")

            if not isinstance(md_content, str) or not md_content.strip():
                md_content = f"# AI Performance Audit (empty)\n_Generated: {ts}_\n"

            ai_fname = out_dir / f"ai_performance_audit_report_daily_{ts}.md"
            _atomic_write_text(ai_fname, md_content)
            results["ai_decision_report_path"] = str(ai_fname)
        except Exception as e:
            results["errors"].append(f"AI report error: {e}")

        # ---------- 3) Rapport Mecano (JSON) ----------
        try:
            # a) Paramétrage de la période si exposée
            if hasattr(mecano, "set_period_days"):
                mecano.set_period_days(period_days)
            elif hasattr(mecano, "report_period_days"):
                try:
                    setattr(mecano, "report_period_days", period_days)
                except Exception:
                    pass

            # b) Génération
            if hasattr(mecano, "build_weekly_report"):
                try:
                    report = mecano.build_weekly_report(period_days=period_days)
                except TypeError:
                    # signature sans argument (par défaut hebdo) → on acceptera le défaut
                    report = mecano.build_weekly_report()
            elif hasattr(mecano, "build_report"):
                report = mecano.build_report(period_days=period_days)
            else:
                raise RuntimeError("Mecano ne fournit pas d’API de génération (build_weekly_report / build_report manquantes).")

            if not isinstance(report, dict):
                report = {"payload": report, "generated_at": ts, "period_days": period_days}

            mec_fname = out_dir / f"mecano_report_daily_{ts}.json"

            # c) Export natif si existe, sinon écriture locale atomique
            exported = False
            if hasattr(mecano, "export_report"):
                try:
                    # certaines implémentations acceptent (report, format, out_path)
                    mecano.export_report(report, format="json", out_path=str(mec_fname))
                    exported = True
                except TypeError:
                    try:
                        mecano.export_report(report, format="json")  # laisser impl décider du nom
                        # si on ne peut pas récupérer le nom, on écrit nous-mêmes
                        if not mec_fname.exists():
                            _atomic_write_json(mec_fname, report)
                    except Exception:
                        _atomic_write_json(mec_fname, report)
                except Exception:
                    _atomic_write_json(mec_fname, report)
            else:
                _atomic_write_json(mec_fname, report)

            results["mecano_report_path"] = str(mec_fname)
        except Exception as e:
            results["errors"].append(f"Mecano report error: {e}")

        # ---------- 4) Log minimal + retour ----------
        try:
            logger = getattr(self, "logger", None)
            if logger:
                logger.info(
                    f"[AI AUDITS] IA='{results['ai_decision_report_path']}' | "
                    f"Mecano='{results['mecano_report_path']}' | errors={len(results['errors'])}"
                )
        except Exception:
            pass

        return results


    def _format_audit_report_for_file(
        self,
        audit_results: Dict[str, Any],
        trading_summary_by_asset: Dict[str, Any],
        period: str,
    ) -> str:
        """
        Formate les résultats de l'audit et le résumé de trading en un rapport Markdown lisible.
        Cette version retire les notions de score de confiance et de priorité de l'IA.
        """
        report_date = datetime.now(UTC).strftime("%Y-%m-%d")
        report_lines = []

        report_lines.append(
            f"# Rapport d'Audit de Performance SNIPER_X - {report_date} ({period.replace('_', ' ').capitalize()})\n"
        )
        report_lines.append(
            f"**Généré par l'IA d'Audit à :** {datetime.now(UTC).strftime('%Y-%m-%d %H:%M:%S UTC')}\n"
        )

        report_lines.append("## 1. Résumé de l'Audit par l'IA\n")
        report_lines.append(
            f"**Objectif de l'Audit :** Analyse des performances de trading sur la période `{period}`.\n"
        )
        report_lines.append(
            f"**Résumé Général :** {audit_results.get('audit_summary', 'Non spécifié.')}\n"
        )

        # Suppression des lignes relatives à la priorité et au niveau de confiance de l'IA
        # report_lines.append(f"**Priorité des Suggestions (IA) :** {audit_results.get('priority', 'N/A'):.2f}\n")
        # report_lines.append(f"**Niveau de Confiance de l'IA :** {audit_results.get('confidence_score', 'N/A'):.2f}\n")

        # Recommandations de l'IA (si présentes)
        if audit_results.get("recommendations"):
            report_lines.append("\n### Recommandations Générales de l'IA :\n")
            for i, rec in enumerate(audit_results["recommendations"]):
                report_lines.append(
                    f"- **{i+1}. {rec.get('title', 'Recommandation')}:** {rec.get('description', 'Pas de description.')}"
                )
                if rec.get("impact"):
                    report_lines.append(f"  *Impact Potentiel :* {rec.get('impact')}")
                if rec.get("effort"):
                    report_lines.append(f"  *Effort Estimé :* {rec.get('effort')}")

        # Métriques agrégées (si présentes)
        if audit_results.get("metrics"):
            report_lines.append("\n### Métriques Clés Analysées par l'IA :\n")
            for metric, value in audit_results["metrics"].items():
                if isinstance(value, (int, float)):
                    report_lines.append(
                        f"- **{metric.replace('_', ' ').title()}:** {value:.2f}"
                    )
                else:
                    report_lines.append(
                        f"- **{metric.replace('_', ' ').title()}:** {value}"
                    )

        report_lines.append("\n## 2. Bilan de Trading Détaillé par Actif et Phase\n")
        if not trading_summary_by_asset:
            report_lines.append("Aucun trade enregistré pour la période analysée.")
        else:
            for asset, summary in trading_summary_by_asset.items():
                report_lines.append(f"### Actif : {asset}\n")
                report_lines.append(f"- **Trades Totaux :** {summary['total_trades']}")
                report_lines.append(
                    f"- **Gains / Pertes :** {summary['wins']} / {summary['losses']}"
                )
                report_lines.append(
                    f"- **P&L Net Total :** ${summary['total_pnl']:.2f}\n"
                )

                report_lines.append("  **Performance par Phase de Marché :**\n")
                if summary["pnl_by_phase"]:
                    for phase, data in summary["pnl_by_phase"].items():
                        win_rate_phase = (
                            round((data["wins"] / data["trades"]) * 100, 2)
                            if data["trades"] > 0
                            else 0.0
                        )
                        report_lines.append(
                            f"    - **Phase `{phase}` :** {data['trades']} trades | P&L: ${data['total_pnl']:.2f} | Taux de Gain: {win_rate_phase:.2f}%"
                        )
                else:
                    report_lines.append(
                        "    Aucune performance détaillée par phase disponible."
                    )
                report_lines.append("\n---")

        report_lines.append(
            "\n## 3. Détails des Trades Analysés par l'IA (Échantillon)\n"
        )
        report_lines.append(
            "*(Le détail complet des trades doit être extrait des logs pertinents pour une analyse approfondie et se trouve dans les fichiers de logs bruts si nécessaire.)*\n"
        )

        report_lines.append("\n--- Fin du Rapport d'Audit ---\n")

        return "\n".join(report_lines)

    def _save_audit_report_to_file(self, report_content: str, period: str) -> Path:
        """
        Sauvegarde le contenu du rapport d'audit dans un fichier Markdown horodaté
        dans le dossier configuré pour les audits IA.
        - Utilise self.ai_audit_reports_dir si défini, sinon paths.ai_audit,
        sinon <paths.configs>/ai_audit, fallback final: "config/ai_audit".
        - Écriture atomique (temp file + rename) pour éviter les fichiers corrompus.
        """
        from pathlib import Path
        from datetime import datetime, UTC
        from tempfile import NamedTemporaryFile

        # --------- 1) Résolution du dossier de sortie (robuste) ---------
        try:
            base_configs_dir = Path(self.config_manager.get("paths.configs", "config"))
        except Exception:
            base_configs_dir = Path("config")

        # priorité à un attribut d'instance s'il est correctement défini (et non vide)
        ai_dir = None
        try:
            ai_dir_attr = getattr(self, "ai_audit_reports_dir", None)
            if ai_dir_attr:
                ai_dir = Path(str(ai_dir_attr))
        except Exception:
            ai_dir = None

        if not ai_dir:
            # essaye la clé dédiée si présente
            try:
                ai_dir_cfg = self.config_manager.get("paths.ai_audit", None)
                if ai_dir_cfg:
                    ai_dir = Path(str(ai_dir_cfg))
            except Exception:
                ai_dir = None

        if not ai_dir:
            # fallback vers <configs>/ai_audit
            ai_dir = base_configs_dir / "ai_audit"

        # Assure l'existence
        ai_dir.mkdir(parents=True, exist_ok=True)

        # --------- 2) Nom de fichier horodaté ---------
        period_safe = (period or "last_day")
        try:
            period_safe = period_safe.replace(" ", "_").replace("/", "-")
        except Exception:
            period_safe = "last_day"

        timestamp = datetime.now(UTC).strftime("%Y%m%d_%H%M%S")
        report_filename = f"ai_performance_audit_report_{period_safe}_{timestamp}.md"
        report_file_path = ai_dir / report_filename

        # --------- 3) Écriture atomique ---------
        tmp_path = None
        try:
            # NamedTemporaryFile avec delete=False pour compat Windows (rename après fermeture)
            with NamedTemporaryFile("w", delete=False, dir=str(ai_dir), encoding="utf-8") as tmp:
                tmp.write(report_content if isinstance(report_content, str) else str(report_content))
                tmp_path = Path(tmp.name)

            # Remplacement atomique
            tmp_path.replace(report_file_path)
            self.logger.info(f"AIDecision: Rapport d'audit sauvegardé dans '{report_file_path}'.")
        except Exception as e:
            self.logger.error(
                f"AIDecision: Échec de la sauvegarde du rapport d'audit vers '{report_file_path}': {e}",
                exc_info=True,
            )
            # best-effort cleanup
            try:
                if tmp_path and tmp_path.exists():
                    tmp_path.unlink(missing_ok=True)
            except Exception:
                pass
            # Alerte robuste (signature à 2 paramètres)
            try:
                if self.config_manager:
                    self.config_manager.send_alert(
                        message=f"AI Audit Report Save Fail: {e}",
                        alert_type="telegram_critical",
                    )
            except TypeError:
                # fallback positionnel si nécessaire
                try:
                    self.config_manager.send_alert(f"AI Audit Report Save Fail: {e}", "telegram_critical")
                except Exception:
                    pass

        return report_file_path


    def suggest_trading_improvements(
        self, logs: List[Dict[str, Any]], context: Dict[str, Any]
    ) -> Dict[str, Any]:
        """
        Analyse les logs historiques de trading et le contexte actuel pour formuler des
        suggestions d'améliorations des stratégies ou du comportement du bot.
        Utilise un prompt externalisé et une taille d'échantillon de logs configurable.
        Cette fonction est essentielle pour le rôle d'auditeur et de conseiller de l'IA,
        fournissant des insights pour l'optimisation manuelle.
        L'IA ne calcule AUCUN score de confiance ou de priorité.

        Args:
            logs (List[Dict[str, Any]]): Liste des entrées de logs de trading à analyser.
            context (Dict[str, Any]): Le contexte système et de marché actuel.

        Returns:
            Dict[str, Any]: Une analyse et une suggestion d'amélioration de trading générées par l'IA.
                            Inclut 'error' en cas d'échec.
        """
        self.logger.info(
            "AIDecision: Génération d'analyses et de suggestions d'améliorations de trading..."
        )

        prompt_template = self.prompts.get("suggest_trading_improvements")
        if not prompt_template:
            self.logger.error(
                "AIDecision: Le prompt 'suggest_trading_improvements' est manquant. Impossible d'analyser et de suggérer des améliorations."
            )
            if self.config_manager:
                self.config_manager.send_alert(
                    "CRITIQUE",
                    f"AI Prompt Manquant: suggest_trading_improvements",
                    "telegram_critical",
                )
            return {"error": "Prompt 'suggest_trading_improvements' non configuré."}

        # Lire la taille d'échantillon de logs depuis la configuration
        log_sample_size = self.config_manager.get(
            "ai.supervisor_settings.log_sample_size", 100
        )
        self.logger.debug(
            f"AIDecision: Utilisation d'une taille d'échantillon de logs de {log_sample_size} pour l'analyse IA."
        )

        # Formater le prompt avec les données réelles (les 'log_sample_size' dernières entrées de logs)
        try:
            logs_json = (
                json.dumps(
                    logs[-log_sample_size:],
                    indent=2,
                    cls=self.config_manager.CustomJSONEncoder,
                )
                if logs
                else "None"
            )
            context_json = json.dumps(
                context, indent=2, cls=self.config_manager.CustomJSONEncoder
            )
        except Exception as e:
            self.logger.error(
                f"AIDecision: Erreur de sérialisation des logs ou du contexte pour le prompt 'suggest_trading_improvements': {e}.",
                exc_info=True,
            )
            if self.config_manager:
                self.config_manager.send_alert(
                    "CRITIQUE",
                    f"AI: Erreur sérialisation prompt suggest_trading_improvements: {e}",
                    "telegram_critical",
                )
            return {"error": f"Erreur de sérialisation pour le prompt: {e}"}

        prompt = prompt_template.format(logs=logs_json, context=context_json)

        raw_response = self._generate_raw_response(prompt)
        suggestion_analysis = self.parse_response(raw_response)

        if "error" not in suggestion_analysis:
            suggestion_analysis["suggestion_type"] = "trading_improvement_analysis"
            self._add_suggestion_to_history(suggestion_analysis)

            # Notification Telegram (sans priorité ni score de confiance)
            self.notify_telegram(
                "new_suggestion",
                {
                    "type": "Trading Improvement Analysis",
                    "summary": suggestion_analysis.get(
                        "summary",
                        "Nouvelle analyse et suggestion d'amélioration de trading",
                    ),
                    "explanation": suggestion_analysis.get("explanation", "N/A"),
                },
            )
        return suggestion_analysis

    def simulate_strategy(
        self,
        strategy_obj: "BaseStrategy",
        context: Dict[str, Any],
        historical_data: List[Dict[str, Any]],
    ) -> Dict[str, Any]:
        """
        Demande à l'IA de simuler l'application d'un objet de stratégie sur des données historiques.
        Cette fonction est utilisée pour l'évaluation et l'optimisation des stratégies.
        L'IA ne calcule AUCUN score de confiance.

        Args:
            strategy_obj (BaseStrategy): L'instance de la stratégie à simuler.
            context (Dict[str, Any]): Le contexte de marché et système actuel.
            historical_data (List[Dict[str, Any]]): Un échantillon des données historiques pertinentes.

        Returns:
            Dict[str, Any]: Les résultats de la simulation générés par l'IA. Inclut 'error' en cas d'échec.
        """
        # Obtenir le nom et les paramètres sérialisables de l'objet stratégie
        strategy_name = str(strategy_obj)
        strategy_params = strategy_obj.get_parameters()

        self.logger.info(f"AIDecision: Simulation de la stratégie '{strategy_name}'...")

        prompt_template = self.prompts.get("simulate_strategy")
        if not prompt_template:
            self.logger.error(
                "AIDecision: Le prompt 'simulate_strategy' est manquant. Impossible de simuler la stratégie."
            )
            if self.config_manager:
                self.config_manager.send_alert(
                    "CRITIQUE",
                    "AI Prompt Manquant: simulate_strategy",
                    "telegram_critical",
                )
            return {"error": "Prompt 'simulate_strategy' non configuré."}

        # Lire la taille de l'échantillon des données historiques depuis la configuration
        historical_data_sample_size = self.config_manager.get(
            "ai.supervisor_settings.historical_data_sample_size", 10
        )
        self.logger.debug(
            f"AIDecision: Utilisation d'un échantillon de {historical_data_sample_size} points de données historiques pour l'IA."
        )

        # Formater le prompt avec les données (l'échantillon configuré des dernières entrées)
        try:
            strategy_params_json = json.dumps(
                strategy_params, indent=2, cls=self.config_manager.CustomJSONEncoder
            )
            historical_data_json = (
                json.dumps(
                    historical_data[-historical_data_sample_size:],
                    indent=2,
                    cls=self.config_manager.CustomJSONEncoder,
                )
                if historical_data
                else "None"
            )
            context_json = json.dumps(
                context, indent=2, cls=self.config_manager.CustomJSONEncoder
            )
        except Exception as e:
            self.logger.error(
                f"AIDecision: Erreur de sérialisation pour le prompt 'simulate_strategy': {e}.",
                exc_info=True,
            )
            if self.config_manager:
                self.config_manager.send_alert(
                    "CRITIQUE",
                    f"AI: Erreur sérialisation prompt simulate_strategy: {e}",
                    "telegram_critical",
                )
            return {"error": f"Erreur de sérialisation pour le prompt: {e}"}

        prompt = prompt_template.format(
            strategy=strategy_params_json,
            context=context_json,
            historical_data=historical_data_json,
        )

        raw_response = self._generate_raw_response(prompt)

        try:
            simulation_result = self.parse_response(raw_response)
            if "error" in simulation_result:
                self.logger.error(
                    f"AIDecision: Erreur de parsing du résultat de simulation: {simulation_result['error']}. Réponse brute: {raw_response[:200]}..."
                )
                if self.config_manager:
                    self.config_manager.send_alert(
                        "ERREUR_AI_PARSE",
                        f"AI: Erreur parsing simulate_strategy: {simulation_result['error']}",
                        "telegram_critical",
                    )
                return simulation_result

            # S'assurer que les clés essentielles sont présentes, avec des valeurs par défaut
            simulation_result.setdefault("strategy_name", strategy_name)
            simulation_result.setdefault("net_pnl", 0.0)
            simulation_result.setdefault("max_drawdown", 0.0)
            simulation_result.setdefault("win_rate", 0.0)
            simulation_result.setdefault("total_trades", 0)
            simulation_result.setdefault(
                "sim_details", "Résultats simulés basés sur la compréhension de l'IA."
            )

            self.logger.info(
                f"AIDecision: Simulation pour '{strategy_name}' terminée. Net PnL: {simulation_result.get('net_pnl', 0.0):.2f}."
            )
            # Journaliser la simulation sans score de confiance
            self.log_analysis_event(
                analysis_details=simulation_result,
                analysis_type="Strategy Simulation",
                context_summary={"strategy": strategy_name, "period": "historical"},
            )
            return simulation_result
        except Exception as e:
            self.logger.error(
                f"AIDecision: Échec du traitement du résultat de simulation: {e}. Réponse brute: {raw_response[:200]}...",
                exc_info=True,
            )
            if self.config_manager:
                self.config_manager.send_alert(
                    "CRITIQUE",
                    f"AI: Échec traitement simulate_strategy: {e}",
                    "telegram_critical",
                )
            return {
                "error": "Échec du traitement de la simulation",
                "raw_response": raw_response,
            }

    def compare_strategies(
        self, strategies: List["BaseStrategy"], context: Dict[str, Any]
    ) -> Dict[str, Any]:
        """
        Demande à l'IA de comparer plusieurs objets de stratégie basés sur leurs paramètres
        et le contexte de marché, en utilisant un prompt externalisé.
        L'IA ne calcule AUCUN score de confiance.

        Args:
            strategies (List[BaseStrategy]): Une liste d'instances de stratégies à comparer.
            context (Dict[str, Any]): Le contexte de marché et système actuel pour l'analyse comparative.

        Returns:
            Dict[str, Any]: Un dictionnaire de résultats de comparaison généré par l'IA.
                            Inclut 'error' en cas d'échec.
        """
        self.logger.info("AIDecision: Comparaison d'objets de stratégie en cours...")

        prompt_template = self.prompts.get("compare_strategies")
        if not prompt_template:
            self.logger.error(
                "AIDecision: Le prompt 'compare_strategies' est manquant. Impossible de comparer les stratégies."
            )
            if self.config_manager:
                self.config_manager.send_alert(
                    "CRITIQUE",
                    "AI Prompt Manquant: compare_strategies",
                    "telegram_critical",
                )
            return {"error": "Prompt 'compare_strategies' non configuré."}

        # Extraire les paramètres de chaque objet de stratégie pour la sérialisation
        strategy_params_list = [
            strategy_obj.get_parameters()
            for strategy_obj in strategies
            if isinstance(strategy_obj, BaseStrategy)
        ]

        if not strategy_params_list:
            self.logger.error(
                "Aucun objet de stratégie valide fourni pour la comparaison."
            )
            return {
                "error": "La liste des stratégies à comparer est vide ou ne contient pas d'objets valides."
            }

        # Formater le prompt avec les données des stratégies et le contexte
        try:
            strategies_json = json.dumps(
                strategy_params_list,
                indent=2,
                cls=self.config_manager.CustomJSONEncoder,
            )
            context_json = json.dumps(
                context, indent=2, cls=self.config_manager.CustomJSONEncoder
            )
        except Exception as e:
            self.logger.error(
                f"AIDecision: Erreur de sérialisation pour le prompt 'compare_strategies': {e}.",
                exc_info=True,
            )
            if self.config_manager:
                self.config_manager.send_alert(
                    "CRITIQUE",
                    f"AI: Erreur sérialisation prompt compare_strategies: {e}",
                    "telegram_critical",
                )
            return {"error": f"Erreur de sérialisation pour le prompt: {e}"}

        prompt = prompt_template.format(
            strategies=strategies_json, context=context_json
        )

        raw_response = self._generate_raw_response(prompt)

        try:
            comparison_result = self.parse_response(raw_response)
            if "error" in comparison_result:
                self.logger.error(
                    f"AIDecision: Erreur de parsing du résultat de comparaison: {comparison_result['error']}. Réponse brute: {raw_response[:200]}..."
                )
                if self.config_manager:
                    self.config_manager.send_alert(
                        "ERREUR_AI_PARSE",
                        f"AI: Erreur parsing compare_strategies: {comparison_result['error']}",
                        "telegram_critical",
                    )
                return comparison_result

            # S'assurer que les clés essentielles sont présentes
            comparison_result.setdefault("best_strategy", "none")
            comparison_result.setdefault("rationale", "Analyse de l'IA.")
            comparison_result.setdefault("comparison_summary", {})
            self.logger.info(
                f"AIDecision: Comparaison de stratégies terminée. Stratégie recommandée: '{comparison_result['best_strategy']}'."
            )
            # Journaliser la comparaison sans score de confiance
            self.log_analysis_event(
                analysis_details=comparison_result,
                analysis_type="Strategy Comparison",
                context_summary={"strategies_compared": [str(s) for s in strategies]},
            )
            return comparison_result
        except Exception as e:
            self.logger.error(
                f"AIDecision: Échec du traitement du résultat de comparaison: {e}. Réponse brute: {raw_response[:200]}...",
                exc_info=True,
            )
            if self.config_manager:
                self.config_manager.send_alert(
                    "CRITIQUE",
                    f"AI: Échec traitement compare_strategies: {e}",
                    "telegram_critical",
                )
            return {
                "error": "Échec du traitement de la comparaison",
                "raw_response": raw_response,
            }

    def review_code(
        self, code_snippet: str, context: Optional[Dict[str, Any]] = None
    ) -> Dict[str, Any]:
        """
        Passe en revue un extrait de code fourni par l'utilisateur ou le système,
        en utilisant un prompt externalisé. Génère des suggestions d'amélioration de code.
        Cette fonction est cruciale pour la qualité du code et la maintenabilité du bot.
        L'IA ne calcule AUCUN score de confiance ou de priorité.

        Args:
            code_snippet (str): L'extrait de code à analyser.
            context (Optional[Dict[str, Any]]): Contexte additionnel (ex: nom du fichier, section).

        Returns:
            Dict[str, Any]: Un dictionnaire de revue de code généré par l'IA.
                            Inclut 'error' en cas d'échec.
        """
        self.logger.info("AIDecision: Revue de code par l'IA en cours...")

        prompt_template = self.prompts.get("review_code")
        if not prompt_template:
            self.logger.error(
                "AIDecision: Le prompt 'review_code' est manquant. Impossible de revoir le code."
            )
            if self.config_manager:
                self.config_manager.send_alert(
                    "CRITIQUE",
                    "AI Prompt Manquant: review_code",
                    "telegram_critical",
                )
            return {"error": "Prompt 'review_code' non configuré."}

        # Formater le prompt avec l'extrait de code et le contexte additionnel
        try:
            prompt = prompt_template.format(
                code_snippet=code_snippet,
                context_info=(
                    json.dumps(
                        context, indent=2, cls=self.config_manager.CustomJSONEncoder
                    )
                    if context
                    else "None"
                ),
            )
        except Exception as e:
            self.logger.error(
                f"AIDecision: Erreur de sérialisation du contexte pour le prompt 'review_code': {e}.",
                exc_info=True,
            )
            if self.config_manager:
                self.config_manager.send_alert(
                    "CRITIQUE",
                    f"AI: Erreur sérialisation prompt review_code: {e}",
                    "telegram_critical",
                )
            return {"error": f"Erreur de sérialisation pour le prompt: {e}"}

        raw_response = self._generate_raw_response(prompt)
        review = self.parse_response(raw_response)

        if "error" not in review:
            review["suggestion_type"] = "code_review"
            self._add_suggestion_to_history(review)
            # Notification Telegram (sans priorité)
            self.notify_telegram(
                "new_suggestion",
                {
                    "type": "Code Review",
                    "summary": review.get("review_summary", "Nouvelle revue de code"),
                    "code_snippet_excerpt": code_snippet[:100] + "...",
                },
            )
        return review

    def suggest_dev_improvements(
        self, codebase_path: str, context: Optional[Dict[str, Any]] = None
    ) -> Dict[str, Any]:
        """
        Analyse le codebase (ou une partie) pour formuler des suggestions d'améliorations de développement.
        Utilise un prompt externalisé. La logique d'analyse du codebase peut être réelle
        ou simulée en fonction des outils disponibles.
        Cette fonction renforce le rôle de l'IA comme auditeur et conseiller pour la qualité logicielle.
        L'IA ne calcule AUCUN score de confiance ou de priorité.

        Args:
            codebase_path (str): Le chemin vers la base de code à analyser.
            context (Optional[Dict[str, Any]]): Contexte additionnel (ex: type d'analyse, résultats de tests).

        Returns:
            Dict[str, Any]: Une analyse et une suggestion d'amélioration du développement générées par l'IA.
                            Inclut 'error' en cas d'échec.
        """
        self.logger.info(
            "AIDecision: Analyse et suggestion d'améliorations de développement pour la base de code..."
        )

        prompt_template = self.prompts.get("suggest_dev_improvements")
        if not prompt_template:
            self.logger.error(
                "AIDecision: Le prompt 'suggest_dev_improvements' est manquant. Impossible d'analyser et de suggérer des améliorations de dev."
            )
            if self.config_manager:
                self.config_manager.send_alert(
                    "CRITIQUE",
                    f"AI Prompt Manquant: suggest_dev_improvements",
                    "telegram_critical",
                )
            return {"error": "Prompt 'suggest_dev_improvements' non configuré."}

        # TODO: Remplacer cette simulation par une analyse réelle du codebase (ex: en utilisant des outils d'analyse statique)
        #       Pour cela, le ConfigManager pourrait orchestrer l'exécution d'outils externes
        #       (ex: SonarQube, Bandit, Pylint) et passer leurs résultats ici.
        simulated_analysis = {
            "files_scanned": [
                "ai_decision.py",
                "config_manager.py",
                "trade_executor.py",
                "phase_observer.py",
            ],
            "directory_structure_depth": 3,
            "potential_circular_dependencies": ["Aucune détectée (simulé)"],
            "test_coverage_estimate": "basse (simulé)",
            "documentation_completeness_estimate": "moyenne (simulé)",
            "last_analysis_date": datetime.now(UTC).isoformat(),
        }
        # Inclure le contexte dans le prompt
        try:
            prompt = prompt_template.format(
                simulated_analysis=json.dumps(
                    simulated_analysis,
                    indent=2,
                    cls=self.config_manager.CustomJSONEncoder,
                ),
                context_info=(
                    json.dumps(
                        context, indent=2, cls=self.config_manager.CustomJSONEncoder
                    )
                    if context
                    else "None"
                ),
            )
        except Exception as e:
            self.logger.error(
                f"AIDecision: Erreur de sérialisation du contexte pour le prompt 'suggest_dev_improvements': {e}.",
                exc_info=True,
            )
            if self.config_manager:
                self.config_manager.send_alert(
                    "CRITIQUE",
                    f"AI: Erreur sérialisation prompt suggest_dev_improvements: {e}",
                    "telegram_critical",
                )
            return {"error": f"Erreur de sérialisation pour le prompt: {e}"}

        raw_response = self._generate_raw_response(prompt)
        suggestion_analysis = self.parse_response(raw_response)

        if "error" not in suggestion_analysis:
            suggestion_analysis["suggestion_type"] = "dev_improvement_analysis"
            self._add_suggestion_to_history(suggestion_analysis)
            # Notification Telegram (sans priorité)
            self.notify_telegram(
                "new_suggestion",
                {
                    "type": "Dev Improvement Analysis",
                    "summary": suggestion_analysis.get(
                        "summary",
                        "Nouvelle analyse et suggestion d'amélioration de développement",
                    ),
                    "risks_identified": suggestion_analysis.get("risks_identified", []),
                },
            )
        return suggestion_analysis

    def analyze_dependencies(
        self, project_path: str, context: Optional[Dict[str, Any]] = None
    ) -> Dict[str, Any]:
        """
        Effectue une analyse des dépendances du projet (ex: depuis `requirements.txt`) pour identifier
        les mises à jour disponibles, les vulnérabilités de sécurité et les dépendances inutiles.
        Utilise un prompt externalisé pour la génération de suggestions par l'IA.
        La logique d'analyse est simulée pour l'instant, en attendant l'intégration d'outils externes.
        Cette fonction renforce le rôle de l'IA en tant qu'auditeur de la santé technique du projet.
        L'IA ne calcule AUCUN score de confiance ou de priorité.

        Args:
            project_path (str): Le chemin vers la racine du projet à analyser.
            context (Optional[Dict[str, Any]]): Contexte additionnel (ex: environnement, date du dernier scan).

        Returns:
            Dict[str, Any]: Une analyse et une suggestion d'audit de dépendances générées par l'IA.
                            Inclut 'error' en cas d'échec.
        """
        self.logger.info(
            "AIDecision: Analyse des dépendances du projet en cours de génération de suggestions..."
        )

        prompt_template = self.prompts.get("analyze_dependencies")
        if not prompt_template:
            self.logger.error(
                "AIDecision: Le prompt 'analyze_dependencies' est manquant. Impossible d'analyser et de suggérer des améliorations de dépendances."
            )
            if self.config_manager:
                self.config_manager.send_alert(
                    "CRITIQUE",
                    "AI Prompt Manquant: analyze_dependencies",
                    "telegram_critical",
                )
            return {"error": "Prompt 'analyze_dependencies' non configuré."}

        # TODO: Remplacer cette simulation par une analyse réelle des dépendances
        #       (ex: en lisant requirements.txt, en utilisant des outils d'analyse de vulnérabilités comme `pip-audit`)
        #       Ceci devrait être orchestré par Mecano qui passerait les résultats ici.
        simulated_dependencies = {
            "requirements_txt_exists": Path(project_path, "requirements.txt").is_file(),
            "dependencies_sample": {
                "numpy": {
                    "version": "1.23.5",
                    "latest": "1.26.0",
                    "security_vulnerabilities_count": 0,
                },
                "requests": {
                    "version": "2.28.1",
                    "latest": "2.31.0",
                    "security_vulnerabilities_count": 1,
                },
            },
            "unnecessary_dependencies_found": ["matplotlib (simulé)"],
            "last_scan_date": datetime.now(UTC).isoformat(),
        }

        # Formater le prompt avec les données des dépendances, en utilisant CustomJSONEncoder
        try:
            prompt = prompt_template.format(
                simulated_dependencies=json.dumps(
                    simulated_dependencies,
                    indent=2,
                    cls=self.config_manager.CustomJSONEncoder,
                ),
                context_info=(
                    json.dumps(
                        context, indent=2, cls=self.config_manager.CustomJSONEncoder
                    )
                    if context
                    else "None"
                ),
            )
        except Exception as e:
            self.logger.error(
                f"AIDecision: Erreur de sérialisation du contexte pour le prompt 'analyze_dependencies': {e}.",
                exc_info=True,
            )
            if self.config_manager:
                self.config_manager.send_alert(
                    "CRITIQUE",
                    f"AI: Erreur sérialisation prompt analyze_dependencies: {e}",
                    "telegram_critical",
                )
            return {"error": f"Erreur de sérialisation pour le prompt: {e}"}

        raw_response = self._generate_raw_response(prompt)
        suggestion_analysis = self.parse_response(raw_response)

        if "error" not in suggestion_analysis:
            suggestion_analysis["suggestion_type"] = "dependency_audit_analysis"
            self._add_suggestion_to_history(suggestion_analysis)
            # Notification Telegram (sans priorité)
            self.notify_telegram(
                "new_suggestion",
                {
                    "type": "Dependency Audit Analysis",
                    "summary": suggestion_analysis.get(
                        "summary",
                        "Nouvelle analyse et suggestion d'audit de dépendances",
                    ),
                    "vulnerabilities_found_count": sum(
                        d.get("security_vulnerabilities_count", 0)
                        for d in simulated_dependencies["dependencies_sample"].values()
                    ),
                },
            )
        return suggestion_analysis

    def audit_project(
        self, project_path: str, context: Optional[Dict[str, Any]] = None
    ) -> Dict[str, Any]:
        """
        Conduit un audit de haut niveau du projet (structure, processus, risques) en utilisant un prompt externalisé.
        La logique d'audit est simulée pour l'instant.
        Cette fonction renforce le rôle de l'IA en tant que superviseur et conseiller architectural.
        L'IA ne calcule AUCUN score de confiance ou de priorité.

        Args:
            project_path (str): Le chemin vers la racine du projet à auditer.
            context (Optional[Dict[str, Any]]): Contexte additionnel (ex: objectifs de l'audit, période).

        Returns:
            Dict[str, Any]: Une analyse et une suggestion d'audit de projet générées par l'IA.
                            Inclut 'error' en cas d'échec.
        """
        self.logger.info(
            "AIDecision: Démarrage de l'analyse d'audit de projet par l'IA..."
        )

        prompt_template = self.prompts.get("audit_project")
        if not prompt_template:
            self.logger.error(
                "AIDecision: Le prompt 'audit_project' est manquant. Impossible de réaliser l'analyse d'audit du projet."
            )
            if self.config_manager:
                self.config_manager.send_alert(
                    "CRITIQUE",
                    "AI Prompt Manquant: audit_project",
                    "telegram_critical",
                )
            return {"error": "Prompt 'audit_project' non configuré."}

        # TODO: Remplacer cette simulation par une analyse réelle du projet (scanning de fichiers, structure, etc.)
        #       Ceci impliquerait de lire la structure du répertoire, de compter les lignes de code, etc.
        simulated_audit_findings = {
            "modularity_score": 0.8,
            "version_control_system": "Git",
            "deployment_strategy": "Manual pull on VPS (simulé)",
            "logging_strategy": "Excellent",
            "monitoring_strategy": "Good (Mecano)",
            "identified_risks": [
                "Single Point of Failure (VPS)",
                "Lack of automated testing",
            ],
            "last_audit_date": datetime.now(UTC).isoformat(),
        }

        # Formater le prompt avec les résultats d'audit simulés, en utilisant CustomJSONEncoder
        try:
            prompt = prompt_template.format(
                simulated_audit_findings=json.dumps(
                    simulated_audit_findings,
                    indent=2,
                    cls=self.config_manager.CustomJSONEncoder,
                ),
                context_info=(
                    json.dumps(
                        context, indent=2, cls=self.config_manager.CustomJSONEncoder
                    )
                    if context
                    else "None"
                ),
            )
        except Exception as e:
            self.logger.error(
                f"AIDecision: Erreur de sérialisation du contexte pour le prompt 'audit_project': {e}.",
                exc_info=True,
            )
            if self.config_manager:
                self.config_manager.send_alert(
                    "CRITIQUE",
                    f"AI: Erreur sérialisation prompt audit_project: {e}",
                    "telegram_critical",
                )
            return {"error": f"Erreur de sérialisation pour le prompt: {e}"}

        raw_response = self._generate_raw_response(prompt)
        audit_analysis_and_suggestion = self.parse_response(raw_response)

        if "error" not in audit_analysis_and_suggestion:
            audit_analysis_and_suggestion["suggestion_type"] = "project_audit_analysis"
            self._add_suggestion_to_history(audit_analysis_and_suggestion)
            # Notification Telegram (sans priorité)
            self.notify_telegram(
                "new_suggestion",
                {
                    "type": "Project Audit Analysis",
                    "summary": audit_analysis_and_suggestion.get(
                        "audit_summary",
                        "Nouvelle analyse d'audit de projet et suggestion",
                    ),
                    "risks_identified_count": len(
                        audit_analysis_and_suggestion.get("identified_risks", [])
                    ),
                },
            )
        return audit_analysis_and_suggestion

    def generate_project_documentation(
        self, project_path: str, context: Optional[Dict[str, Any]] = None
    ) -> Dict[str, Any]:
        """
        Analyse les besoins de documentation du projet pour générer des suggestions d'amélioration,
        en utilisant un prompt externalisé. La logique d'analyse est simulée pour l'instant.
        Cette fonction renforce le rôle de l'IA comme conseiller et auditeur de la complétude documentaire.
        L'IA ne calcule AUCUN score de confiance ou de priorité.

        Args:
            project_path (str): Le chemin vers la racine du projet à documenter.
            context (Optional[Dict[str, Any]]): Contexte additionnel (ex: modules spécifiques, types de docs).

        Returns:
            Dict[str, Any]: Une analyse et une suggestion d'amélioration de documentation générées par l'IA.
                            Inclut 'error' en cas d'échec.
        """
        self.logger.info(
            "AIDecision: Démarrage de l'analyse et la génération de suggestions pour la documentation du projet..."
        )

        prompt_template = self.prompts.get("generate_project_documentation")
        if not prompt_template:
            self.logger.error(
                "AIDecision: Le prompt 'generate_project_documentation' est manquant. Impossible d'analyser et de suggérer de la documentation."
            )
            if self.config_manager:
                self.config_manager.send_alert(
                    "CRITIQUE",
                    "AI Prompt Manquant: generate_project_documentation",
                    "telegram_critical",
                )
            return {"error": "Prompt 'generate_project_documentation' non configuré."}

        # TODO: Remplacer cette simulation par une analyse réelle des besoins en documentation (ex: scan des docstrings manquants)
        simulated_doc_needs = {
            "missing_docstrings_modules": ["trade_executor", "mecano", "ai_decision"],
            "api_endpoints_undocumented": False,
            "system_architecture_diagram_missing": True,
            "onboarding_guide_completeness": "basse",
            "last_doc_scan_date": datetime.now(UTC).isoformat(),
        }

        # Formater le prompt avec les besoins en documentation simulés, en utilisant CustomJSONEncoder
        try:
            prompt = prompt_template.format(
                simulated_doc_needs=json.dumps(
                    simulated_doc_needs,
                    indent=2,
                    cls=self.config_manager.CustomJSONEncoder,
                ),
                context_info=(
                    json.dumps(
                        context, indent=2, cls=self.config_manager.CustomJSONEncoder
                    )
                    if context
                    else "None"
                ),
            )
        except Exception as e:
            self.logger.error(
                f"AIDecision: Erreur de sérialisation du contexte pour le prompt 'generate_project_documentation': {e}.",
                exc_info=True,
            )
            if self.config_manager:
                self.config_manager.send_alert(
                    "CRITIQUE",
                    f"AI: Erreur sérialisation prompt generate_project_documentation: {e}",
                    "telegram_critical",
                )
            return {"error": f"Erreur de sérialisation pour le prompt: {e}"}

        raw_response = self._generate_raw_response(prompt)
        documentation_suggestion_analysis = self.parse_response(raw_response)

        if "error" not in documentation_suggestion_analysis:
            documentation_suggestion_analysis["suggestion_type"] = (
                "documentation_improvement_analysis"
            )
            self._add_suggestion_to_history(documentation_suggestion_analysis)
            # Notification Telegram (sans priorité)
            self.notify_telegram(
                "new_suggestion",
                {
                    "type": "Documentation Improvement Analysis",
                    "summary": documentation_suggestion_analysis.get(
                        "summary", "Nouvelle analyse et suggestion de documentation"
                    ),
                    "gaps_identified_count": len(
                        documentation_suggestion_analysis.get("gaps_identified", [])
                    ),
                },
            )
        return documentation_suggestion_analysis

    def check_compliance(
        self, context: Dict[str, Any], project_path: str
    ) -> Dict[str, Any]:
        """
        Conduit une analyse d'audit de la conformité du projet (RGPD, sécurité, standards de trading)
        en utilisant un prompt externalisé. La logique d'audit est simulée pour l'instant,
        en attendant l'intégration d'outils externes.
        Cette fonction renforce le rôle de l'IA comme auditeur de conformité et superviseur des risques.
        L'IA ne calcule AUCUN score de confiance ou de priorité.

        Args:
            context (Dict[str, Any]): Le contexte système et de marché actuel.
            project_path (str): Le chemin vers la racine du projet à auditer.

        Returns:
            Dict[str, Any]: Une analyse et une suggestion d'audit de conformité générées par l'IA.
                            Inclut 'error' en cas d'échec.
        """
        self.logger.info(
            "AIDecision: Démarrage de l'analyse d'audit de conformité du projet..."
        )

        prompt_template = self.prompts.get("check_compliance")
        if not prompt_template:
            self.logger.error(
                "AIDecision: Le prompt 'check_compliance' est manquant. Impossible d'analyser l'audit de conformité."
            )
            if self.config_manager:
                self.config_manager.send_alert(
                    "CRITIQUE",
                    "AI Prompt Manquant: check_compliance",
                    "telegram_critical",
                )
            return {"error": "Prompt 'check_compliance' non configuré."}

        # TODO: Remplacer cette simulation par une analyse réelle des problèmes de conformité.
        # Cela pourrait impliquer:
        # - Lecture de documents de politique de données
        # - Scanning du code pour des patterns non conformes (ex: non-anonymisation des données)
        # - Vérification des logs d'audit pour des violations (intégration avec Mecano)
        simulated_compliance_issues = {
            "gdpr_compliance": "partial_data_anonymization_needed",
            "security_best_practices": "weak_api_key_management (to be fixed with .env)",
            "trading_standards": "insufficient_audit_trail_for_manual_overrides",
            "data_retention_policy": "undefined",
            "last_compliance_audit_date": datetime.now(UTC).isoformat(),
        }

        # Formater le prompt avec les données de conformité simulées, en utilisant CustomJSONEncoder
        try:
            prompt = prompt_template.format(
                context=json.dumps(
                    context, indent=2, cls=self.config_manager.CustomJSONEncoder
                ),
                simulated_compliance_issues=json.dumps(
                    simulated_compliance_issues,
                    indent=2,
                    cls=self.config_manager.CustomJSONEncoder,
                ),
            )
        except Exception as e:
            self.logger.error(
                f"AIDecision: Erreur de sérialisation du contexte pour le prompt 'check_compliance': {e}.",
                exc_info=True,
            )
            if self.config_manager:
                self.config_manager.send_alert(
                    "CRITIQUE",
                    f"AI: Erreur sérialisation prompt check_compliance: {e}",
                    "telegram_critical",
                )
            return {"error": f"Erreur de sérialisation pour le prompt: {e}"}

        raw_response = self._generate_raw_response(prompt)
        compliance_audit_analysis_and_suggestion = self.parse_response(raw_response)

        if "error" not in compliance_audit_analysis_and_suggestion:
            compliance_audit_analysis_and_suggestion["suggestion_type"] = (
                "compliance_audit_analysis"
            )
            self._add_suggestion_to_history(compliance_audit_analysis_and_suggestion)
            # Notification Telegram (sans priorité)
            self.notify_telegram(
                "compliance_alert",
                {
                    "issue": compliance_audit_analysis_and_suggestion.get(
                        "audit_summary", "Problème de conformité détecté"
                    ),
                    "item": "Projet SNIPER_X",
                    "action": compliance_audit_analysis_and_suggestion.get(
                        "recommended_actions", ["Revoir et corriger"]
                    )[0],
                    "risks_identified_count": len(
                        compliance_audit_analysis_and_suggestion.get("risks", [])
                    ),
                },
            )
        return compliance_audit_analysis_and_suggestion

    def explain_decision(
        self, decision: Dict[str, Any], context: Dict[str, Any]
    ) -> str:
        """
        Fournit une explication de qualité institutionnelle pour une suggestion générée par l'IA.
        Le niveau de détail de l'explication peut varier en fonction du type de suggestion.
        Utilise un prompt externalisé. L'IA ne calcule AUCUN score de confiance.

        Args:
            decision (Dict[str, Any]): Le dictionnaire de suggestion généré par l'IA.
            context (Dict[str, Any]): Le contexte de marché et système au moment de la génération.

        Returns:
            str: Une explication détaillée et formatée.
        """
        self.logger.info(
            "AIDecision: Génération de l'explication de la suggestion IA..."
        )

        explanation_template_key = self.config_manager.get(
            "ai.prompt_settings.explain_decision_template_key", "explain_suggestion"
        )  # Renommé la clé pour clarté
        prompt_template = self.prompts.get(explanation_template_key)

        if not prompt_template:
            self.logger.error(
                f"AIDecision: Le prompt d'explication '{explanation_template_key}' est manquant. Retourne une explication générique."
            )
            return "Explication générique: L'IA n'a pas pu générer une explication détaillée en raison d'un prompt manquant."

        try:
            decision_json = json.dumps(
                decision, indent=2, cls=self.config_manager.CustomJSONEncoder
            )
            context_json_for_explanation = json.dumps(
                context, indent=2, cls=self.config_manager.CustomJSONEncoder
            )
        except Exception as e:
            self.logger.error(
                f"AIDecision: Erreur de sérialisation des données pour le prompt d'explication: {e}.",
                exc_info=True,
            )
            return "Erreur lors de la sérialisation des données pour l'explication."

        try:
            prompt = prompt_template.format(
                decision=decision_json, context=context_json_for_explanation
            )
        except KeyError as e:
            self.logger.error(
                f"AIDecision: Clé manquante dans le template d'explication: {e}. Vérifiez le prompt YAML."
            )
            return "Erreur de formatage de l'explication: Clé de template manquante."

        raw_explanation_response = self._generate_raw_response(prompt)

        self.logger.info("AIDecision: Explication générée par l'IA.")
        return raw_explanation_response

    def log_analysis_event(
        self,
        analysis_details: Dict[str, Any],
        analysis_type: str,
        context_summary: Dict[str, Any],
        ai_input_trace: Optional[Dict[str, Any]] = None,
    ) -> None:
        """
        Journalise de manière exhaustive une analyse ou une suggestion générée par l'IA.
        Cette entrée de log est cruciale pour l'auditabilité et le post-analyse.
        Elle remplace l'ancienne `log_decision` pour refléter le rôle de conseiller de l'IA.

        Args:
            analysis_details (Dict[str, Any]): Le dictionnaire de l'analyse/suggestion générée par l'IA.
            analysis_type (str): Le type d'analyse ou d'événement (ex: "Performance Audit", "Code Review").
            context_summary (Dict[str, Any]): Un résumé du contexte de marché et système au moment de l'analyse.
                                                (Note: Pour des logs très volumineux, un résumé du contexte est effectué).
            ai_input_trace (Optional[Dict[str, Any]]): L'input de l'IA qui a influencé l'analyse (optionnel).
        """
        # Journalise un résumé concis à la console
        summary_text = analysis_details.get(
            "summary", analysis_details.get("analysis_type", "N/A")
        )
        self.logger.info(
            f"AIDecision: Analyse Journalisée: Type='{analysis_type}', Résumé='{summary_text}'."
        )

        # Création d'un résumé du contexte pour le log, pour éviter d'enregistrer les DataFrames complets
        summarized_context_for_log = context_summary.copy()

        # Traitement spécifique pour 'market_data' qui contient des DataFrames
        if "market_data" in summarized_context_for_log and isinstance(
            summarized_context_for_log["market_data"], dict
        ):
            summarized_market_data_for_log = {}
            for asset, data_entry in summarized_context_for_log["market_data"].items():
                if isinstance(data_entry, pd.DataFrame) and not data_entry.empty:
                    # Conserver seulement les dernières valeurs clés de PhaseObserver
                    last_row = data_entry.iloc[-1].to_dict()
                    summarized_market_data_for_log[asset] = {
                        "phase": last_row.get("phase"),
                        "confidence_score": last_row.get(
                            "confidence_score"
                        ),  # Maintenu ici pour le log, même si l'IA ne l'utilise plus directement pour sa décision
                        "current_price": last_row.get("close"),
                        "volume_momentum": last_row.get("volume_momentum"),
                        "nearest_liquidity_level_details": last_row.get(
                            "nearest_liquidity_level_details"
                        ),
                        "entry_confirmation_bullish": last_row.get(
                            "entry_confirmation_bullish", False
                        ),
                        "entry_confirmation_bearish": last_row.get(
                            "entry_confirmation_bearish", False
                        ),
                        "is_liquid": last_row.get("is_liquid", True),
                        "current_spread_points": last_row.get("current_spread_points"),
                        "last_update_timestamp": last_row.get("last_update_timestamp"),
                    }
                else:
                    summarized_market_data_for_log[asset] = "Dataframe vide ou invalide"
            summarized_context_for_log["market_data_summary"] = (
                summarized_market_data_for_log
            )
            del summarized_context_for_log["market_data"]

        # Traitement spécifique pour 'open_positions' pour éviter des logs trop verbeux
        if "open_positions" in summarized_context_for_log and isinstance(
            summarized_context_for_log["open_positions"], list
        ):
            summarized_context_for_log["open_positions_count"] = len(
                summarized_context_for_log["open_positions"]
            )
            summarized_context_for_log["open_positions_summary"] = [
                {
                    "ticket": pos.get("ticket"),
                    "symbol": pos.get("symbol"),
                    "type": pos.get("type"),
                    "volume": pos.get("volume"),
                }
                for pos in summarized_context_for_log["open_positions"][:5]
            ]
            del summarized_context_for_log["open_positions"]

        # Nettoyage des clés potentiellement volumineuses dans le contexte, configurables via `app.context_keys_to_clean_for_logs`
        keys_to_clean_from_context = self.config_manager.get(
            "app.context_keys_to_clean_for_logs",
            ["economic_calendar", "ai_recommendation_score", "asset_configs"],
        )
        for key in keys_to_clean_from_context:
            if key in summarized_context_for_log:
                if isinstance(summarized_context_for_log[key], (list, dict)):
                    summarized_context_for_log[f"{key}_summary"] = (
                        f"Contenu (taille {len(summarized_context_for_log[key])}) non logué pour optimisation des logs."
                    )
                else:
                    summarized_context_for_log[f"{key}_summary"] = (
                        summarized_context_for_log[key]
                    )
                del summarized_context_for_log[key]

        log_entry = {
            "timestamp": datetime.now(UTC).isoformat(),
            "event_type": analysis_type,  # Utilise le type d'analyse
            "analysis_details": analysis_details,  # Les détails de l'analyse générée par l'IA
            "context_at_analysis": summarized_context_for_log,  # Utilise le contexte résumé
            "model_info": {"name": self.model_path, "mode": "local_llama"},
            "ai_input_for_trace": ai_input_trace if ai_input_trace is not None else {},
        }

        # Sauvegarde l'entrée complète et détaillée dans un fichier d'audit dédié (JSONL)
        try:
            log_dir = Path(self.config_manager.get("paths.logs", "logs/"))
            log_file_name = self.config_manager.get(
                "paths.ai_supervisor_logs_file", "ai_supervisor_logs.jsonl"
            )

            full_log_path = log_dir / log_file_name
            os.makedirs(log_dir, exist_ok=True)

            with open(full_log_path, "a", encoding="utf-8") as f:
                f.write(
                    json.dumps(log_entry, cls=self.config_manager.CustomJSONEncoder)
                    + "\n"
                )
            self.logger.debug(
                f"AIDecision: Log d'analyse détaillé sauvegardé vers '{full_log_path}'."
            )
        except Exception as e:
            self.logger.error(
                f"AIDecision: Échec de l'écriture du log d'analyse détaillé vers '{full_log_path}': {e}",
                exc_info=True,
            )
            if self.config_manager:
                self.config_manager.send_alert(
                    "CRITIQUE",
                    f"AI Log Analyse Échec: {e}",
                    "telegram_critical",
                )

    def _add_suggestion_to_history(self, suggestion: Dict[str, Any]) -> None:
        """
        Ajoute une nouvelle suggestion/analyse générée par l'IA à l'historique interne du superviseur.
        Chaque suggestion reçoit un ID unique robuste (UUID).
        Le statut initial de la suggestion est lu dynamiquement depuis la configuration.
        Après ajout, la fonction sauvegarde l'historique sur disque.
        L'IA n'associe AUCUN score de confiance ou de priorité à ses suggestions.

        Args:
            suggestion (Dict[str, Any]): Le dictionnaire de la suggestion/analyse AI à ajouter.
        """
        suggestion_id = str(uuid.uuid4())
        timestamp = datetime.now(UTC).isoformat()

        default_status = self.config_manager.get(
            "ai.suggestion_settings.default_initial_status", "pending"
        )  # Récupérer directement depuis config_manager
        # Note: self.default_initial_suggestion_status peut être conservé si vous préférez le charger une seule fois dans __init__

        history_entry = {
            "id": suggestion_id,
            "timestamp": timestamp,
            "suggestion_details": suggestion,
            "status": default_status,
            "last_reminded": None,
        }
        self.suggestion_history.append(history_entry)

        self._save_suggestion_history()

        log_context = {
            "suggestion_id_created": suggestion_id,
            "suggestion_type": suggestion.get("suggestion_type", "N/A"),
        }

        # Journalise l'événement d'ajout de la suggestion via self.log_analysis_event (sans score)
        self.log_analysis_event(
            analysis_details=suggestion,
            analysis_type=f"Suggestion Added: {suggestion.get('suggestion_type', 'N/A')}",
            context_summary=log_context,
            ai_input_trace=None,  # Pas d'input trace direct pour l'ajout à l'historique
        )
        self.logger.info(
            f"AIDecision: Suggestion '{suggestion_id}' ajoutée à l'historique. Type: {suggestion.get('suggestion_type', 'N/A')}."
        )

    def get_suggestion_history(
        self, filter_by: Optional[str] = None
    ) -> List[Dict[str, Any]]:
        """
        Récupère l'historique des suggestions/analyses AI, avec une option de filtre par statut.
        Assure le chargement initial de l'historique si non déjà fait.
        L'IA ne calcule AUCUN score de confiance.

        Args:
            filter_by (Optional[str]): Statut par lequel filtrer ('pending', 'approved', 'rejected', 'reminded').
                                    Si `None`, toutes les suggestions sont retournées.

        Returns:
            List[Dict[str, Any]]: Une liste d'entrées historiques de suggestions/analyses.
        """
        # Assurez-vous que l'historique est chargé si ce n'est pas déjà fait (ex: après un rechargement ConfigManager)
        if not self.suggestion_history and Path(self.history_path).exists():
            self.logger.debug(
                "AIDecision: Rechargement de l'historique des suggestions/analyses car il est vide."
            )
            self._load_suggestion_history()

        if filter_by:
            # Laisser le code commenté pour rappel si une énumération de statut est ajoutée.
            # if filter_by not in [status.value for status in SuggestionStatus]:
            #     self.logger.warning(f"AIDecision: Statut de filtre invalide: {filter_by}. Retourne une liste vide.")
            #     return []

            filtered = [
                s for s in self.suggestion_history if s.get("status") == filter_by
            ]
            self.logger.info(
                f"AIDecision: Récupéré {len(filtered)} suggestions/analyses filtrées par statut: '{filter_by}'."
            )
            return filtered
        self.logger.info(
            f"AIDecision: Récupéré toutes les {len(self.suggestion_history)} suggestions/analyses de l'historique."
        )
        return self.suggestion_history

    def remind_pending_suggestions(self) -> None:
        """
        Vérifie les suggestions/analyses en attente qui n'ont pas encore été traitées
        et envoie des rappels via Telegram si nécessaire.
        Les paramètres de la logique de rappel sont lus dynamiquement depuis la configuration.
        L'IA ne calcule AUCUN score de confiance ou de priorité.
        """
        # Accéder au ConfigManager pour obtenir les paramètres de rappel
        # Suppression de `min_priority` car l'IA ne gère plus les priorités
        remind_after_hours = self.config_manager.get(
            "ai.supervisor_settings.remind_after_hours", 24
        )
        reminder_multiplier = self.config_manager.get(
            "ai.supervisor_settings.reminder_multiplier", 0.5
        )

        pending_suggestions = self.get_suggestion_history(filter_by="pending")
        now = datetime.now(UTC)
        reminded_count = 0

        self.logger.info(
            f"AIDecision: Vérification des suggestions/analyses en attente ({len(pending_suggestions)} trouvées)..."
        )

        for suggestion_entry in pending_suggestions:
            suggestion = suggestion_entry["suggestion_details"]
            # Suppression du recalcul de la priorité ou de son utilisation
            # suggestion_priority = suggestion.get("priority", self.score_decision(suggestion, {}))

            # if suggestion_priority >= min_priority: # Cette condition est supprimée
            suggestion_timestamp = datetime.fromisoformat(
                suggestion_entry["timestamp"]
            ).astimezone(UTC)
            hours_since_creation = (now - suggestion_timestamp).total_seconds() / 3600

            if hours_since_creation >= remind_after_hours:
                last_reminded_str = suggestion_entry.get("last_reminded")
                if last_reminded_str:
                    last_reminded_time = datetime.fromisoformat(
                        last_reminded_str
                    ).astimezone(UTC)
                    if (
                        now - last_reminded_time
                    ).total_seconds() / 3600 < remind_after_hours * reminder_multiplier:
                        self.logger.debug(
                            f"AIDecision: Suggestion/analyse {suggestion_entry['id']} déjà rappelé récemment. Ignoré."
                        )
                        continue

                self.notify_telegram(
                    "pending_suggestion_reminder",
                    {
                        "summary": suggestion.get(
                            "summary",
                            f"Suggestion/Analyse ID: {suggestion_entry['id']}",
                        ),
                        "age": int(hours_since_creation),
                        "type": suggestion.get("suggestion_type", "N/A"),
                        "id": suggestion_entry["id"],
                    },
                )
                suggestion_entry["last_reminded"] = now.isoformat()
                suggestion_entry["status"] = "reminded"

                # Journalise l'envoi du rappel via self.log_analysis_event (sans score)
                self.log_analysis_event(
                    analysis_details=suggestion,
                    analysis_type=f"Reminder Sent: {suggestion.get('suggestion_type', 'N/A')}",
                    context_summary={
                        "age_hours": hours_since_creation,
                        "reminder_sent": True,
                    },
                    ai_input_trace=None,
                )
                reminded_count += 1
            else:
                self.logger.debug(
                    f"AIDecision: Suggestion/analyse {suggestion_entry['id']} pas encore due pour un rappel."
                )

        self.logger.info(
            f"AIDecision: Vérification des suggestions/analyses en attente terminée. {reminded_count} rappels envoyés."
        )

   
    def _feedback_safe(self, suggestion: dict, result: dict):
        """
        Envoie le feedback à l'IA avec la signature correcte :
        feedback_on_result(suggestion, result).
        """
        ai = getattr(self.config_manager, "ai_decision_instance", None)
        if not ai:
            return
        try:
            return ai.feedback_on_result(suggestion, result)
        except Exception as e:
            self.logger.warning(f"[AI FEEDBACK] Impossible d'envoyer le feedback: {e}")

    def adapt_strategy(self, feedback_data: List[Dict[str, Any]]) -> Dict[str, Any]:
        """
        Demande à l'IA d'adapter les stratégies de trading en se basant sur un ensemble de feedbacks historiques.
        Utilise un prompt externalisé et agrège les données de feedback.
        L'IA ne calcule AUCUN score de confiance.

        Args:
            feedback_data (List[Dict[str, Any]]): Liste des entrées de feedback (résultats de trades/suggestions).

        Returns:
            Dict[str, Any]: Un rapport d'adaptation de stratégie généré par l'IA.
                            Inclut 'error' en cas d'échec.
        """
        self.logger.info(
            "AIDecision: Demande d'adaptation de stratégie basée sur le feedback historique en cours..."
        )

        prompt_template = self.prompts.get("adapt_strategy")
        if not prompt_template:
            self.logger.error(
                "AIDecision: Le prompt 'adapt_strategy' est manquant. Impossible d'adapter la stratégie."
            )
            if self.config_manager:
                self.config_manager.send_alert(
                    "CRITIQUE",
                    f"AI Prompt Manquant: adapt_strategy",
                    "telegram_critical",
                )
            return {"error": "Prompt 'adapt_strategy' non configuré."}

        # Logique d'analyse et d'agrégation du feedback
        wins = sum(
            1
            for f in feedback_data
            if f.get("outcome", {}).get("status") == "executed"
            and f.get("outcome", {}).get("pnl_usd", 0) > 0
        )
        losses = sum(
            1
            for f in feedback_data
            if f.get("outcome", {}).get("status") == "executed"
            and f.get("outcome", {}).get("pnl_usd", 0) < 0
        )
        applied_suggestions = sum(
            1 for f in feedback_data if f.get("outcome", {}).get("status") == "applied"
        )
        total_feedback_entries = len(feedback_data)

        # Résumé agrégé pour le prompt
        aggregated_summary = f"Total_Feedbacks={total_feedback_entries}, Gains_Trades={wins}, Pertes_Trades={losses}, Suggestions_Appliquees={applied_suggestions}."

        # Échantillon des derniers feedbacks bruts pour donner du contexte à l'IA
        raw_sample_size = self.config_manager.get(
            "ai.supervisor_settings.feedback_raw_sample_size", 5
        )

        try:
            raw_sample_json = (
                json.dumps(
                    feedback_data[-raw_sample_size:],
                    indent=2,
                    cls=self.config_manager.CustomJSONEncoder,
                )
                if feedback_data
                else "None"
            )
        except Exception as e:
            self.logger.error(
                f"AIDecision: Erreur de sérialisation de l'échantillon de feedback pour le prompt 'adapt_strategy': {e}.",
                exc_info=True,
            )
            if self.config_manager:
                self.config_manager.send_alert(
                    "CRITIQUE",
                    f"AI: Erreur sérialisation prompt adapt_strategy: {e}",
                    "telegram_critical",
                )
            raw_sample_json = "Erreur de sérialisation."

        prompt = prompt_template.format(
            aggregated_feedback=aggregated_summary, raw_feedback_sample=raw_sample_json
        )

        raw_response = self._generate_raw_response(prompt)
        adaptation_report = self.parse_response(raw_response)

        if "error" not in adaptation_report:
            self.logger.info(
                f"AIDecision: Stratégie AI Adaptée: {adaptation_report.get('adaptation_summary', 'Pas de résumé')}."
            )
            # Journaliser l'adaptation de stratégie sans score de confiance
            self.log_analysis_event(
                analysis_details=adaptation_report,
                analysis_type="Strategy Adaptation Report",
                context_summary={
                    "feedback_count": total_feedback_entries,
                    "aggregated_summary": aggregated_summary,
                },
            )

        return adaptation_report

    def feedback_on_result(
        self, decision: Dict[str, Any], result: Dict[str, Any]
    ) -> None:
        """
        Incorpore le résultat d'un trade ou d'une suggestion AI (feedback) pour la traçabilité
        et l'apprentissage futur du superviseur AI. Met à jour l'historique des suggestions
        et journalise le feedback de manière sécurisée.

        Args:
            decision (Dict[str, Any]): La décision ou suggestion AI originale.
            result (Dict[str, Any]): Le résultat de cette décision/suggestion (ex: statut d'exécution du trade, P&L).
        """
        decision_summary = decision.get("summary", decision.get("trade_type", "N/A"))
        result_status = result.get("status", "N/A")
        self.logger.info(
            f"AIDecision: Feedback reçu pour: '{decision_summary}' avec le résultat: '{result_status}'."
        )

        # Mettre à jour le statut dans l'historique des suggestions si l'ID est présent
        suggestion_id = decision.get("id")
        if suggestion_id:
            found_suggestion = False
            for entry in self.suggestion_history:
                if entry.get("id") == suggestion_id:
                    entry["status"] = result.get("execution_status", "unknown").lower()
                    entry["feedback_result"] = result
                    self.logger.info(
                        f"AIDecision: Statut de la suggestion '{suggestion_id}' mis à jour à '{entry['status']}'."
                    )
                    found_suggestion = True
                    break
            if not found_suggestion:
                self.logger.warning(
                    f"AIDecision: Suggestion avec ID '{suggestion_id}' non trouvée dans l'historique pour le feedback. Historique pourrait être désynchronisé."
                )

        # Préparer l'entrée de log pour le journal d'audit de feedback AI
        feedback_data = {
            "timestamp": datetime.now(UTC).isoformat(),
            "event_type": "ai_feedback",
            "original_decision": decision,
            "outcome": result,
            "model_info": {"name": self.model_path, "mode": "local_llama"},
        }

        # Écrire dans le fichier de log de feedback de manière sécurisée (JSONL)
        try:
            log_dir = Path(self.log_dir)
            log_file_name = self.ai_supervisor_feedback_file

            full_log_path = log_dir / log_file_name
            os.makedirs(log_dir, exist_ok=True)

            # Correction: Ajout du bloc 'with open' pour définir 'f'
            with open(
                full_log_path, "a", encoding="utf-8"
            ) as f:  # Ajout du bloc with open
                # CustomJSONEncoder est importé localement dans la fonction.
                f.write(
                    json.dumps(feedback_data, cls=self.config_manager.CustomJSONEncoder)
                    + "\n"
                )
            self.logger.debug(
                f"AIDecision: Log de feedback pour '{decision_summary}' sauvegardé vers '{full_log_path}'."
            )
        except Exception as e:
            self.logger.error(
                f"AIDecision: Échec de l'écriture du log de feedback vers '{full_log_path}': {e}",
                exc_info=True,
            )
            if self.config_manager:
                self.config_manager.send_alert(
                    "CRITIQUE",
                    f"AI Feedback Log Échec: {e}",
                    "telegram_critical",
                )

    def adapt_strategy(self, feedback_data: List[Dict[str, Any]]) -> Dict[str, Any]:
        """
        Demande à l'IA d'adapter les stratégies de trading en se basant sur un ensemble de feedbacks historiques.
        Utilise un prompt externalisé et agrège les données de feedback.
        Cette fonction est le cœur de l'apprentissage adaptatif du bot.

        Args:
            feedback_data (List[Dict[str, Any]]): Liste des entrées de feedback (résultats de trades/suggestions).

        Returns:
            Dict[str, Any]: Un rapport d'adaptation de stratégie généré par l'IA.
                            Inclut 'error' en cas d'échec.
        """
        self.logger.info(
            "AIDecision: Demande d'adaptation de stratégie basée sur le feedback historique en cours..."
        )  # Utilise self.logger

        prompt_template = self.prompts.get("adapt_strategy")
        if not prompt_template:
            self.logger.error(
                "AIDecision: Le prompt 'adapt_strategy' est manquant. Impossible d'adapter la stratégie."
            )  # Utilise self.logger
            if self.config_manager:
                self.config_manager.send_alert(
                    "CRITIQUE",
                    "AI Prompt Manquant: adapt_strategy",
                    "telegram_critical",
                )
            return {"error": "Prompt 'adapt_strategy' non configuré."}

        # Logique d'analyse et d'agrégation du feedback
        # Ces métriques sont cruciales pour l'IA et pour le reporting.
        wins = sum(
            1
            for f in feedback_data
            if f.get("outcome", {}).get("status") == "executed"
            and f.get("outcome", {}).get("pnl_usd", 0) > 0
        )
        losses = sum(
            1
            for f in feedback_data
            if f.get("outcome", {}).get("status") == "executed"
            and f.get("outcome", {}).get("pnl_usd", 0) < 0
        )
        # Assumer 'applied' pour les suggestions qui ont été acceptées manuellement
        applied_suggestions = sum(
            1 for f in feedback_data if f.get("outcome", {}).get("status") == "applied"
        )
        total_feedback_entries = len(feedback_data)

        # Résumé agrégé pour le prompt
        aggregated_summary = f"Total_Feedbacks={total_feedback_entries}, Gains_Trades={wins}, Pertes_Trades={losses}, Suggestions_Appliquees={applied_suggestions}."

        # Utiliser `self.config_manager.CustomJSONEncoder` pour la sérialisation robuste
        try:
            # Échantillon des derniers feedbacks bruts pour donner du contexte à l'IA
            raw_sample_size = self.config_manager.get(
                "ai.supervisor_settings.feedback_raw_sample_size", 5
            )  # Nouveau paramètre
            raw_sample_json = (
                json.dumps(
                    feedback_data[-raw_sample_size:],
                    indent=2,
                    cls=self.config_manager.CustomJSONEncoder,
                )
                if feedback_data
                else "None"
            )
        except Exception as e:
            self.logger.error(
                f"AIDecision: Erreur de sérialisation de l'échantillon de feedback pour le prompt 'adapt_strategy': {e}.",
                exc_info=True,
            )
            if self.config_manager:
                self.config_manager.send_alert(
                    "CRITIQUE",
                    f"AI: Erreur sérialisation prompt adapt_strategy: {e}",
                    "telegram_critical",
                )
            raw_sample_json = "Erreur de sérialisation."

        prompt = prompt_template.format(
            aggregated_feedback=aggregated_summary, raw_feedback_sample=raw_sample_json
        )

        raw_response = self._generate_raw_response(prompt)
        adaptation_report = self.parse_response(raw_response)

        if "error" not in adaptation_report:
            self.logger.info(
                f"AIDecision: Stratégie AI Adaptée: {adaptation_report.get('adaptation_summary', 'Pas de résumé')}."
            )  # Utilise self.logger
            # Journalise l'adaptation de stratégie
            self.log_decision(
                adaptation_report,
                {
                    "feedback_count": total_feedback_entries,
                    "aggregated_summary": aggregated_summary,
                },
                "Adaptation de stratégie initiée",
                adaptation_report.get("confidence_score"),
            )

        return adaptation_report

    def notify_telegram(
        self, event_type: str, details: Dict[str, Any]
    ) -> None:  # Retire chat_id, bot_token de la signature
        """
        Envoie une notification formatée à un canal Telegram spécifique.
        Vérifie si le type d'alerte spécifique est activé via ConfigManager avant d'envoyer.
        Les identifiants Telegram sont récupérés directement depuis ConfigManager.

        Args:
            event_type (str): Type d'événement (ex: "new_suggestion", "trade_confirmed", "compliance_alert").
            details (Dict[str, Any]): Dictionnaire avec les détails spécifiques à l'événement.
        """
        # Récupérer les identifiants Telegram via ConfigManager
        bot_token = self.config_manager.get("env_vars.TELEGRAM_BOT_TOKEN")
        chat_id = self.config_manager.get("env_vars.TELEGRAM_CHAT_ID")

        if not chat_id or not bot_token:
            self.logger.warning(
                "AIDecision: Identifiant de chat Telegram ou bot token non défini. Saut de la notification Telegram."
            )  # Utilise self.logger
            return

        # Vérification de l'activation du canal via ConfigManager
        if not self.config_manager.get("telegram.enabled", False):
            self.logger.debug(
                "AIDecision: Telegram est globalement désactivé. Saut de la notification."
            )  # Utilise self.logger
            return

        channel_key = f"telegram.channels.{event_type}"
        channel_enabled = self.config_manager.get(channel_key, False)
        if not channel_enabled:
            self.logger.debug(
                f"AIDecision: Notification Telegram ignorée: Le canal '{event_type}' n'est pas activé dans la configuration."
            )  # Utilise self.logger
            return

        message_template = self.message_templates.get(
            event_type, "Nouvel événement: {event_type} - {details}"
        )

        # Formater le message, gérant les erreurs de formatage (KeyError, etc.)
        try:
            message = message_template.format(event_type=event_type, **details)
        except KeyError as e:
            self.logger.error(
                f"AIDecision: Erreur de formatage du template de notification pour '{event_type}'. Clé manquante: {e}. Message générique utilisé.",
                exc_info=True,
            )  # Utilise self.logger
            message = f"AIDecision: Problème de template pour '{event_type}'. Détails: {details}"  # Message de fallback
        except Exception as e:
            self.logger.error(
                f"AIDecision: Erreur inattendue lors du formatage du message Telegram pour '{event_type}': {e}.",
                exc_info=True,
            )  # Utilise self.logger
            message = f"AIDecision: Erreur inattendue lors du formatage du message. Détails: {details}"  # Message de fallback

        telegram_api_base_url = self.config_manager.get(
            "telegram.api_base_url", "https://api.telegram.org/bot{token}/sendMessage"
        )
        telegram_url = telegram_api_base_url.format(token=bot_token)

        # Lire le parse_mode depuis la configuration
        parse_mode = self.config_manager.get(
            "telegram.parse_mode", "Markdown"
        )  # Parse_mode par défaut 'Markdown'

        payload = {
            "chat_id": chat_id,
            "text": message,
            "parse_mode": parse_mode,
        }

        try:
            response = requests.post(
                telegram_url,
                json=payload,
                timeout=self.config_manager.get("telegram.request_timeout_seconds", 15),
            )  # Ajout d'un timeout configurable
            response.raise_for_status()  # Lève une exception pour les codes de statut HTTP d'erreur
            self.logger.info(
                f"AIDecision: Notification Telegram envoyée avec succès pour l'événement: {event_type}."
            )  # Utilise self.logger
        except requests.exceptions.RequestException as e:
            self.logger.error(
                f"AIDecision: Échec de l'envoi de la notification Telegram pour l'événement '{event_type}': {e}",
                exc_info=True,
            )  # Utilise self.logger
            # Envoyer une alerte critique à un canal d'alerte primaire si les notifications Telegram échouent.
            # Ce log est déjà fait, la send_alert du ConfigManager parent le ferait.
            # Ici, on logue juste l'échec d'envoi de cette notification spécifique.
        except Exception as e:
            self.logger.error(
                f"AIDecision: Une erreur inattendue est survenue lors de l'envoi de la notification Telegram: {e}",
                exc_info=True,
            )  # Utilise self.logger

    def get_ai_status(self) -> Dict[str, Any]:
        """
        Retourne le statut opérationnel actuel du superviseur AI, incluant le modèle chargé,
        son chemin, le résumé de l'activité récente (nombre de suggestions, etc.).

        Returns:
            Dict[str, Any]: Un dictionnaire d'informations sur le statut actuel de l'IA.
        """
        # Récupérer la dernière entrée de log pour un statut plus réel (TODO implémenté)
        last_log_entry_summary = "N/A"
        try:
            log_dir = Path(self.log_dir)
            log_file_name = self.ai_supervisor_logs_file
            full_log_path = log_dir / log_file_name
            if full_log_path.exists():
                with open(
                    full_log_path, "rb"
                ) as f:  # Lire en mode binaire pour lire la dernière ligne
                    f.seek(
                        -2, os.SEEK_END
                    )  # Aller à la fin moins 2 bytes (pour ne pas manquer le dernier \n)
                    while f.read(1) != b"\n":
                        f.seek(-2, os.SEEK_CUR)  # Reculer jusqu'à trouver un \n
                    last_line = f.readline().decode("utf-8").strip()
                last_entry_dict = json.loads(last_line)
                last_log_entry_summary = (
                    last_entry_dict.get("reason", "N/A")
                    + " | "
                    + last_entry_dict.get("generated_item", {}).get("summary", "N/A")[
                        :50
                    ]
                    + "..."
                )
            else:
                self.logger.debug(
                    f"AIDecision: Fichier de log AI '{full_log_path}' non trouvé pour le statut."
                )
        except Exception as e:
            self.logger.error(
                f"AIDecision: Échec de la lecture de la dernière entrée du log AI pour le statut: {e}",
                exc_info=True,
            )
            last_log_entry_summary = f"Erreur lecture log: {type(e).__name__}"

        status = {
            "timestamp": datetime.now(UTC).isoformat(),
            "model_loaded": self.model is not None,
            "model_path": self.model_path,
            "suggestions_in_history": len(self.suggestion_history),
            "pending_suggestions": len(
                self.get_suggestion_history(filter_by="pending")
            ),
            "last_log_entry_summary": last_log_entry_summary,  # Utilise le résumé de la dernière entrée
        }
        self.logger.info("AIDecision: Statut de l'IA demandé.")
        return status

    def pair_programming_session(
        self, user_query: str, code_context: Dict[str, Any]
    ) -> str:
        """
        Engage une session de pair programming avec l'IA. L'utilisateur fournit une requête
        et un contexte de code, et l'IA génère des suggestions ou des corrections.
        Utilise un prompt externalisé.

        Args:
            user_query (str): La question ou la tâche de l'utilisateur.
            code_context (Dict[str, Any]): Contexte du code (ex: 'current_file', 'relevant_code').

        Returns:
            str: La réponse textuelle de l'IA (suggestions, corrections, explications).
        """
        self.logger.info(
            "AIDecision: Démarrage de la session de pair programming avec l'IA..."
        )

        prompt_template = self.prompts.get("pair_programming_prompt")
        if not prompt_template:
            self.logger.error(
                "AIDecision: Le prompt 'pair_programming_prompt' est manquant. Impossible d'engager la session de pair programming."
            )
            if self.config_manager:
                self.config_manager.send_alert(
                    "CRITIQUE",
                    "AI Prompt Manquant: pair_programming_prompt",
                    "telegram_critical",
                )
            return "Erreur : Le prompt de pair programming n'est pas configuré."

        # Formater le prompt avec la requête utilisateur et le contexte du code
        try:
            prompt = prompt_template.format(
                user_query=user_query,
                current_file=code_context.get("current_file", "N/A"),
                relevant_code=code_context.get("relevant_code", "# Aucun code fourni."),
            )
        except (
            KeyError
        ) as e:  # Capturer les erreurs si le template a des placeholders non fournis
            self.logger.error(
                f"AIDecision: Erreur de formatage du prompt 'pair_programming_prompt'. Clé manquante: {e}. Vérifiez le prompt YAML.",
                exc_info=True,
            )
            if self.config_manager:
                self.config_manager.send_alert(
                    "CRITIQUE",
                    f"AI: Erreur formatage prompt pair_programming: {e}",
                    "telegram_critical",
                )
            return "Erreur lors du formatage du prompt de pair programming."
        except Exception as e:
            self.logger.error(
                f"AIDecision: Erreur inattendue lors de la préparation du prompt 'pair_programming_prompt': {e}.",
                exc_info=True,
            )
            return "Erreur lors de la préparation du prompt de pair programming."

        raw_response = self._generate_raw_response(prompt)

        # Journalise l'interaction en mode pair programming
        # Le score sera calculé sur la réponse, si elle est parsable en JSON
        parsed_response_for_score = self.parse_response(
            raw_response
        )  # Tente de parser pour le score

        # Correction: Passer code_context à score_decision au lieu de `context` (qui n'existe pas ici)
        score_for_log = (
            self.score_decision(parsed_response_for_score, code_context)
            if "error" not in parsed_response_for_score
            else None
        )

        self.log_decision(
            {
                "query": user_query,
                "response_excerpt": raw_response[:500] + "...",
            },  # Limiter l'extrait pour le log
            code_context,  # Contexte du code pour l'audit
            "Pair Programming Session",
            score=score_for_log,
        )
        self.logger.info(
            "AIDecision: Session de pair programming terminée. Réponse générée."
        )
        return raw_response  # Retourne la réponse brute

    def train_user(
        self,
        topic: str,
        level: str = "intermediate",
        context: Optional[Dict[str, Any]] = None,
    ) -> str:  # Ajout de context
        """
        Génère du matériel de formation ou des explications sur un sujet donné (trading, code, etc.)
        pour un niveau spécifié, en utilisant un prompt externalisé.

        Args:
            topic (str): Le sujet de la formation.
            level (str): Le niveau de difficulté souhaité (ex: "beginner", "intermediate", "advanced").
            context (Optional[Dict[str, Any]]): Contexte additionnel (ex: besoins spécifiques, erreurs récentes).

        Returns:
            str: Le matériel de formation textuel généré par l'IA.
        """
        self.logger.info(
            f"AIDecision: Génération de matériel de formation sur '{topic}' (Niveau: {level})..."
        )

        prompt_template = self.prompts.get("train_user")
        if not prompt_template:
            self.logger.error(
                "AIDecision: Le prompt 'train_user' est manquant. Impossible de générer du matériel de formation."
            )
            if self.config_manager:
                self.config_manager.send_alert(
                    "CRITIQUE",
                    "AI Prompt Manquant: train_user",
                    "telegram_critical",
                )
            return "Erreur : Le prompt de formation n'est pas configuré."

        try:
            # Formater le prompt avec le sujet, le niveau et le contexte
            prompt = prompt_template.format(
                topic=topic,
                level=level,
                context_info=(
                    json.dumps(
                        context, indent=2, cls=self.config_manager.CustomJSONEncoder
                    )
                    if context
                    else "None"
                ),
            )
        except KeyError as e:
            self.logger.error(
                f"AIDecision: Erreur de formatage du prompt 'train_user'. Clé manquante: {e}. Vérifiez le prompt YAML.",
                exc_info=True,
            )
            return "Erreur lors du formatage du prompt de formation."
        except Exception as e:
            self.logger.error(
                f"AIDecision: Erreur inattendue lors de la préparation du prompt 'train_user': {e}.",
                exc_info=True,
            )
            return "Erreur lors de la préparation du prompt de formation."

        response = self._generate_raw_response(prompt)

        # Journalise la génération de matériel de formation
        parsed_response_for_score = self.parse_response(
            response
        )  # Tente de parser pour le score
        score_for_log = (
            self.score_decision(parsed_response_for_score, context)
            if "error" not in parsed_response_for_score
            else None
        )

        self.log_decision(
            {
                "topic": topic,
                "level": level,
                "response_excerpt": response[:500],
            },  # Limiter l'extrait
            context or {},  # Passe le contexte original ou un dict vide
            "User Training Material Generation",
            score=score_for_log,
        )
        self.logger.info(
            "AIDecision: Matériel de formation généré. Réponse enregistrée."
        )
        return response

    def conduct_multi_agent_vote(
        self,
        problem_statement: str,
        agent_personas: List[Dict[str, Any]],
        context: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """
        Simule un processus de vote ou de consultation avec plusieurs "agents" IA,
        chacun adoptant une persona différente pour analyser un problème donné.
        Les analyses et suggestions sont agrégées pour fournir une recommandation consolidée.
        Utilise des prompts externalisés. Cette fonction renforce le rôle de l'IA comme
        outil d'aide à la décision complexe par consensus.

        Args:
            problem_statement (str): Le problème ou la question à soumettre au vote des agents.
            agent_personas (List[Dict[str, Any]]): Une liste de dictionnaires, chaque dictionnaire décrivant un agent
                                                (ex: {'name': 'RiskManager', 'role': 'expert en gestion des risques'}).
            context (Optional[Dict[str, Any]]): Contexte additionnel (ex: données de marché, état du système).

        Returns:
            Dict[str, Any]: Les résultats de l'analyse et de la recommandation agrégée, y compris la suggestion "gagnante" et la justification.
                            Inclut 'error' en cas d'échec.
        """
        self.logger.info(
            f"AIDecision: Conduite d'une analyse multi-agents pour : '{problem_statement}'."
        )

        # Récupération des templates de prompts
        suggestion_template = self.prompts.get("multi_agent_suggestion")
        voting_template = self.prompts.get(
            "multi_agent_voting"
        )  # Ou 'multi_agent_aggregation' pour plus de clarté
        if not suggestion_template or not voting_template:
            self.logger.error(
                "AIDecision: Un ou plusieurs prompts pour l'analyse multi-agents sont manquants. Impossible de simuler l'analyse."
            )
            if self.config_manager:
                self.config_manager.send_alert(
                    "CRITIQUE",
                    "AI Prompt Manquant: multi_agent_analysis",
                    "telegram_critical",
                )
            return {"error": "Prompts d'analyse non configurés."}

        # Étape 1 : Chaque agent génère une suggestion
        suggestions_from_agents = []  # Renommé pour plus de clarté
        for persona in agent_personas:
            # S'assurer que la persona a un nom et un rôle pour le prompt
            agent_name = persona.get("name", "Agent Inconnu")
            agent_role = persona.get("role", "expert")

            try:
                agent_prompt = suggestion_template.format(
                    agent_name=agent_name,
                    agent_role=agent_role,
                    problem_statement=problem_statement,
                    context_info=(
                        json.dumps(
                            context, indent=2, cls=self.config_manager.CustomJSONEncoder
                        )
                        if context
                        else "None"
                    ),
                )
            except KeyError as e:
                self.logger.error(
                    f"AIDecision: Erreur de formatage du prompt de suggestion d'agent. Clé manquante: {e}. Vérifiez le prompt YAML.",
                    exc_info=True,
                )
                continue
            except Exception as e:
                self.logger.error(
                    f"AIDecision: Erreur inattendue lors de la préparation du prompt d'agent: {e}.",
                    exc_info=True,
                )
                continue

            raw_agent_response = self._generate_raw_response(agent_prompt)
            agent_analysis = self.parse_response(raw_agent_response)  # Renommé

            if "error" not in agent_analysis:
                agent_analysis["agent_name"] = agent_name
                suggestions_from_agents.append(agent_analysis)  # Ajoute l'analyse
                self.logger.info(
                    f"AIDecision: Agent '{agent_name}' a proposé une analyse: {agent_analysis.get('suggestion_summary', 'N/A')}."
                )
            else:
                self.logger.warning(
                    f"AIDecision: Agent '{agent_name}' n'a pas pu générer une analyse valide: {agent_analysis['error']}. Réponse brute: {raw_agent_response[:100]}..."
                )

        if not suggestions_from_agents:
            self.logger.error(
                "AIDecision: Aucune analyse valide n'a été générée par les agents. Analyse consolidée impossible."
            )
            return {"error": "Aucune analyse valide pour la consolidation."}

        # Étape 2 : L'IA évalue les analyses et consolide une recommandation
        try:
            voting_prompt = voting_template.format(
                problem_statement=problem_statement,
                suggestions=json.dumps(
                    suggestions_from_agents,
                    indent=2,
                    cls=self.config_manager.CustomJSONEncoder,
                ),  # Utilise les analyses consolidées
                context_info=(
                    json.dumps(
                        context, indent=2, cls=self.config_manager.CustomJSONEncoder
                    )
                    if context
                    else "None"
                ),
            )
        except Exception as e:
            self.logger.error(
                f"AIDecision: Erreur de sérialisation pour le prompt de consolidation multi-agents: {e}.",
                exc_info=True,
            )
            if self.config_manager:
                self.config_manager.send_alert(
                    "CRITIQUE",
                    f"AI: Erreur sérialisation prompt multi_agent_consolidation: {e}",
                    "telegram_critical",
                )
            return {"error": f"Erreur de sérialisation pour le prompt: {e}"}

        raw_voting_response = self._generate_raw_response(voting_prompt)
        consolidated_recommendation = self.parse_response(
            raw_voting_response
        )  # Renommé

        if "error" not in consolidated_recommendation:
            consolidated_recommendation["suggestion_type"] = (
                "multi_agent_analysis"  # Type de suggestion plus précis
            )
            self._add_suggestion_to_history(
                consolidated_recommendation
            )  # Ajoute à l'historique
            self.log_decision(
                consolidated_recommendation,
                {
                    "problem": problem_statement,
                    "agents": [p["name"] for p in agent_personas],
                },
                "Analyse multi-agents réalisée et consolidée",
                consolidated_recommendation.get("confidence_score"),
            )
            self.logger.info(
                f"AIDecision: Analyse multi-agents terminée. Recommandation consolidée : {consolidated_recommendation.get('winning_suggestion', {}).get('suggestion_summary', 'N/A')}."
            )
        else:
            self.logger.error(
                f"AIDecision: Échec du traitement des résultats de l'analyse multi-agents: {consolidated_recommendation['error']}. Réponse brute: {raw_voting_response[:200]}..."
            )
            if self.config_manager:
                self.config_manager.send_alert(
                    "ERREUR_AI_PARSE",
                    f"AI: Échec parsing multi_agent_analysis: {consolidated_recommendation['error']}",
                    "telegram_critical",
                )
        return consolidated_recommendation  # Retourne l'analyse consolidée

    def send_decision_to_pipeline(self, suggestion_or_analysis: Dict[str, Any]) -> None:
        """
        Simule l'envoi d'une suggestion ou analyse de l'IA vers le pipeline opérationnel du bot.
        Cette fonction souligne l'importance d'une approbation humaine ou d'une validation finale
        par le Config Manager avant toute action concrète (trade, modification de config, etc.).
        Elle confirme que l'IA ne déclenche aucune exécution directe.

        Args:
            suggestion_or_analysis (Dict[str, Any]): La suggestion ou analyse générée par l'IA à "envoyer" pour considération.
        """
        self.logger.info(
            f"AIDecision: Simulation d'envoi d'une suggestion/analyse au pipeline: {suggestion_or_analysis.get('summary', suggestion_or_analysis.get('trade_type', 'N/A'))}."
        )
        # Cette fonction ne déclenche AUCUNE action réelle dans le bot.
        # Elle sert à journaliser l'intention de l'IA de proposer quelque chose au système principal,
        # qui sera ensuite traité (ou non) par le Config Manager ou un opérateur humain.

        # Journaliser la suggestion/analyse envoyée au pipeline
        # Le contexte pour log_decision sera simplifié ici
        self.log_decision(
            suggestion_or_analysis,
            {},
            "Suggestion/Analyse envoyée au pipeline (simulée)",
        )

        # Envoi d'une alerte Telegram pour information
        summary_message = f"Proposition de l'IA envoyée au pipeline: {suggestion_or_analysis.get('summary', suggestion_or_analysis.get('trade_type', 'N/A'))}"
        # Utilise le type de suggestion pour la notification si disponible, sinon "new_suggestion"
        notification_type = suggestion_or_analysis.get(
            "suggestion_type", "new_suggestion"
        )
        self.notify_telegram(
            notification_type,
            {
                "summary": summary_message,
                "priority": suggestion_or_analysis.get("confidence_score", 0.5),
            },
        )

        print(
            f"Suggestion/Analyse '{suggestion_or_analysis.get('summary', suggestion_or_analysis.get('trade_type', 'N/A'))}' envoyée au pipeline pour traitement par le Config Manager et approbation humaine."
        )
