import pandas as pd
from datetime import datetime, UTC, timedelta
import logging
from pathlib import Path
from typing import Dict, Any, List, Optional
import sys
import os
import uuid
import json # Pour sauvegarder les résultats
from itertools import product # Pour générer les combinaisons de paramètres
import copy # Ajout pour la copie profonde des configs

# --- DÉBUT DU BLOC DE CHEMIN PYTHON (IMPORTANT POUR LES IMPORTS) ---
# Ajoute la racine du projet au sys.path pour permettre les imports relatifs
project_root = Path(__file__).resolve().parents[1] # Remonte de trading_tools/optimizer.py vers la racine
if str(project_root) not in sys.path:
    sys.path.insert(0, str(project_root))
# --- FIN DU BLOC DE CHEMIN PYTHON ---

# --- DÉFINITION DU LOGGER POUR L'OPTIMIZER ---
logging.basicConfig(
    level=logging.DEBUG, # Niveau DEBUG pour voir le détail des backtests lancés par l'optimizer.
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger(__name__)

# Imports des modules du projet nécessaires
try:
    from core.config_manager import ConfigManager
    from trading_tools.backtest_engine import BacktestEngine # Import de notre moteur de backtest
    # CustomJSONEncoder est dans core.config_manager, mais nous l'importons ici pour la sérialisation si besoin.
    # Si le ConfigManager n'a pas été modifié, CustomJSONEncoder n'est pas exposé directement,
    # nous devrons donc utiliser une méthode plus simple ou le définir ici si besoin pour la sauvegarde finale.
    # Pour l'instant, on utilise une simple classe si CustomJSONEncoder n'est pas disponible.
    try: # Tente d'importer CustomJSONEncoder si exposé
        from core.config_manager import CustomJSONEncoder
        _json_encoder_class = CustomJSONEncoder
    except ImportError: # Fallback si non exposé
        class _FallbackJSONEncoder(json.JSONEncoder):
            def default(self, obj):
                if isinstance(obj, datetime):
                    return obj.isoformat()
                return json.JSONEncoder.default(self, obj)
        _json_encoder_class = _FallbackJSONEncoder

except ImportError as e:
    logger.critical(f"ERREUR FATALE: Échec de l'importation d'un module essentiel pour l'Optimizer. Erreur: {e}")
    sys.exit(1)


class Optimizer:
    """
    Gère le processus d'optimisation des paramètres de stratégie via le backtesting.
    """

    def __init__(self, config_manager: ConfigManager, backtest_engine_class: type[BacktestEngine]):
        self.config_manager = config_manager # L'instance du ConfigManager global (non modifiée)
        self.backtest_engine_class = backtest_engine_class # La classe BacktestEngine (pour créer de nouvelles instances)
        self.optimization_results = [] # Pour stocker les résultats de chaque combinaison

        # Charger les noms des fichiers CSV des données historiques
        self.symbols_data_map = {
            "EURUSD": "EURUSD_Candlestick_1_M_BID_01.04.2025-19.07.2025.csv",
            "GBPUSD": "GBPUSD_Candlestick_1_M_BID_01.04.2025-19.07.2025.csv",
            "USDCHF": "USDCHF_Candlestick_1_M_BID_01.04.2025-19.07.2025.csv",
            "XAUUSD": "XAUUSD_Candlestick_1_M_BID_01.04.2025-19.07.2025.csv",
            "BTCUSD": "BTCUSD_Candlestick_1_M_BID_01.04.2025-19.07.2025.csv",
            "ETHUSD": "ETHUSD_Candlestick_1_M_BID_01.04.2025-19.07.2025.csv",
            "LTCUSD": "LTCUSD_Candlestick_1_M_BID_01.04.2025-19.07.2025.csv",
        }
        
        # --- NOUVEAU : Charger la prod_config.json originale une fois ---
        self.original_prod_config_path = Path('config') / 'prod_config.json'
        if not self.original_prod_config_path.exists():
            logger.critical(f"FATAL: Le fichier prod_config.json est introuvable à '{self.original_prod_config_path}'.")
            sys.exit(1)
        
        try:
            with open(self.original_prod_config_path, 'r', encoding='utf-8') as f:
                self.base_prod_config = json.load(f)
        except Exception as e:
            logger.critical(f"FATAL: Impossible de lire le fichier prod_config.json: {e}", exc_info=True)
            sys.exit(1)

        logger.info("Optimizer initialisé.")

    def define_parameters_to_optimize(self) -> Dict[str, List[Any]]:
        """
        Définit les paramètres clés pour une stratégie de SCALPING.
        """
        parameters_to_optimize = {
            # Filtres de coût et de liquidité
            "strategies.scalping.opportunity_triggers[0].conditions.current_spread_points.max": [1.0, 1.5],

            # Gestion du risque très serrée
            "strategies.scalping.opportunity_triggers[0].target_sl_pips": [5, 8, 10],
            "strategies.scalping.opportunity_triggers[0].target_tp_pips": [8, 12, 15],

            # Contexte de micro-phase
            "phase_detection_settings.lookback_window": [10, 15, 20],
        }

        logger.info(f"Paramètres à optimiser pour le SCALPING : {parameters_to_optimize}")
        return parameters_to_optimize

    def run_optimization(
        self,
        strategy_name_to_optimize: str, # Quelle stratégie spécifique on optimise (ex: "Scalping Institutional")
        backtest_timeframe: str,
        backtest_start_date: datetime,
        backtest_end_date: datetime
    ) -> List[Dict[str, Any]]:
        """
        Lance le processus d'optimisation en exécutant des backtests pour chaque combinaison de paramètres.

        Args:
            strategy_name_to_optimize (str): Le nom de la stratégie à optimiser.
            backtest_timeframe (str): Le timeframe à utiliser pour les données du backtest.
            backtest_start_date (datetime): La date de début pour la période de backtest.
            backtest_end_date (datetime): La date de fin pour la période de backtest.

        Returns:
            List[Dict[str, Any]]: Une liste des meilleurs résultats d'optimisation (combinaison de paramètres et performance).
        """
        logger.info(f"Démarrage de l'optimisation pour la stratégie '{strategy_name_to_optimize}' sur la période {backtest_start_date.date()} à {backtest_end_date.date()}.")

        params_to_optimize = self.define_parameters_to_optimize()
        
        param_names = list(params_to_optimize.keys())
        param_values = list(params_to_optimize.values())
        
        all_combinations = list(product(*param_values))

        logger.info(f"Total de {len(all_combinations)} combinaisons à tester.")
        
        self.optimization_results = []

        for i, combination in enumerate(all_combinations):
            current_params = dict(zip(param_names, combination))
            logger.info(f"Test de la combinaison {i+1}/{len(all_combinations)}: {current_params}")
            
            # --- DÉBUT DE LA NOUVELLE LOGIQUE DE REINITIALISATION DU CONFIGMANAGER ---
            # Crée une copie de la base prod_config pour cette combinaison
            temp_prod_config_for_this_run = copy.deepcopy(self.base_prod_config)
            
            # Appliquer les paramètres de la combinaison courante à la temp_prod_config
            for param_path, param_value in current_params.items():
                # Utilise un chemin pointé (ex: 'phase_detection_settings.lookback_window')
                parts = param_path.split('.')
                current_level = temp_prod_config_for_this_run
                for part_idx, part in enumerate(parts):
                    if part_idx == len(parts) - 1: # C'est la dernière partie du chemin
                        current_level[part] = param_value
                    else:
                        if part not in current_level or not isinstance(current_level[part], dict):
                            current_level[part] = {} # Créer le dict si manquant
                        current_level = current_level[part]
            
            # Sauvegarder cette config temporaire dans un fichier temporaire
            temp_config_filename = f"prod_config_opt_temp_{uuid.uuid4().hex}.json"
            temp_config_path = Path('output') / temp_config_filename
            temp_config_path.parent.mkdir(parents=True, exist_ok=True) # Assurer que le dossier existe

            try:
                with open(temp_config_path, 'w', encoding='utf-8') as f:
                    json.dump(temp_prod_config_for_this_run, f, indent=4)
            except Exception as e:
                logger.error(f"Échec de la sauvegarde de la config temporaire {temp_config_path}: {e}", exc_info=True)
                continue # Passer à la prochaine combinaison en cas d'erreur grave

            # Réinitialiser/initialiser un NOUVEAU ConfigManager pour ce backtest
            try:
                # Créer une nouvelle instance de ConfigManager à chaque combinaison
                # Ceci assure qu'elle charge la config temporaire pour le backtest
                temp_config_manager_instance = ConfigManager()
                # Initialiser avec le chemin du fichier temporaire
                temp_config_manager_instance.initialize_dynamic_config(
                    template_path=str(temp_config_path),
                    output_path=str(Path('output') / f"dynamic_config_backtest_temp_{uuid.uuid4().hex}.json"), # Autre fichier temporaire
                    config_dir=str(Path('config') / 'strategy') # Assurez-vous que ce chemin est correct
                )
                logger.debug(f"ConfigManager réinitialisé avec la config temporaire pour la combinaison {i+1}.")
            except Exception as e:
                logger.error(f"Erreur lors de la réinitialisation du ConfigManager pour la combinaison {i+1}: {e}", exc_info=True)
                if temp_config_path.exists(): os.remove(temp_config_path) # Nettoyer le fichier temporaire
                continue # Passer à la prochaine combinaison
            
            # Réinitialiser une NOUVELLE instance de BacktestEngine avec le ConfigManager temporaire
            # Cela garantit que chaque backtest utilise la bonne configuration et un état propre.
            temp_backtest_engine_instance = self.backtest_engine_class(config_manager=temp_config_manager_instance)

            # Exécuter le backtest avec les paramètres actuels
            logger.debug(f"Lancement du BacktestEngine pour combinaison {current_params}...")
            
            # Le BacktestEngine.run_backtest doit accepter la config de stratégie temporaire
            # Cela nécessitera de MODIFIER la signature de run_backtest dans backtest_engine.py
            # et la manière dont PhaseObserver et les stratégies obtiennent leurs configs.
            # C'est la clé pour ne pas toucher au ConfigManager global.

            backtest_results = temp_backtest_engine_instance.run_backtest(
                symbols_to_backtest=self.symbols_data_map,
                timeframe=backtest_timeframe,
                start_date=backtest_start_date,
                end_date=backtest_end_date,
                # NOUVEAU PARAMÈTRE : la configuration spécifique pour ce backtest
                strategy_config_override=temp_prod_config_for_this_run 
            )
            
            # Nettoyer le fichier de configuration temporaire après le backtest
            if temp_config_path.exists():
                os.remove(temp_config_path)


            # Stocker les résultats de cette combinaison
            result_for_combination = {
                "parameters": current_params,
                "results": backtest_results
            }
            self.optimization_results.append(result_for_combination)

            # TODO: Ajouter des critères de filtrage de performance ici
            #       (ex: si net_profit_percent > 15% et max_drawdown_percent < 10%)
            #       pour ne stocker que les résultats "prometteurs".

        logger.info("Optimisation terminée. Analyse des résultats...")
        
        return self.optimization_results

    def _generate_optimized_configs(self, output_dir: Path, top_n: int = 5):
        """
        Génère des fichiers de configuration JSON pour les N meilleures combinaisons de paramètres.
        """
        logger.info(f"Génération des {top_n} meilleures configurations optimisées...")
        sorted_results = sorted(
            self.optimization_results,
            key=lambda x: x["results"].get("net_profit_percent", -float('inf')),
            reverse=True
        )
        
        output_dir.mkdir(parents=True, exist_ok=True)

        for i, result in enumerate(sorted_results[:top_n]):
            optimized_params = result["parameters"]
            performance = result["results"]
            
            param_str = "_".join([f"{k.split('.')[-1]}_{v}" for k, v in optimized_params.items()])
            file_name = f"optimized_config_{param_str}_Profit_{performance['net_profit_percent']:.2f}%.json"
            file_path = output_dir / file_name

            # Construire la configuration complète à sauvegarder
            # Reprendre la base_prod_config et lui appliquer les paramètres optimisés
            optimized_strategy_config = copy.deepcopy(self.base_prod_config) # Utiliser la base originale
            
            for param_path, param_value in optimized_params.items():
                parts = param_path.split('.')
                current_level = optimized_strategy_config
                for part_idx, part in enumerate(parts):
                    if part_idx == len(parts) - 1:
                        current_level[part] = param_value
                    else:
                        if part not in current_level or not isinstance(current_level[part], dict):
                            current_level[part] = {}
                        current_level = current_level[part]
            
            try:
                # Utiliser FallbackJSONEncoder pour gérer les objets datetime dans la configuration
                with open(file_path, "w", encoding="utf-8") as f:
                    json.dump(optimized_strategy_config, f, indent=4, cls=_json_encoder_class) # Utilise la classe importée ou fallbackée
                logger.info(f"Configuration optimisée sauvegardée : {file_path}")
            except Exception as e:
                logger.error(f"Échec de la sauvegarde de la config optimisée {file_name}: {e}", exc_info=True)

        logger.info("Génération des fichiers de configuration optimisée terminée.")

# Dans trading_tools/optimizer.py, à la fin du fichier

if __name__ == "__main__":
    # --- Configurer le logging pour ce test spécifique ---
    # Le niveau DEBUG est utile pour voir le détail des backtests lancés par l'optimizer.
    logging.basicConfig(
        level=logging.DEBUG, # Ou logging.INFO pour moins de détails du backtest, mais voir l'avancement de l'optimizer.
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s"
    )
    logger.setLevel(logging.DEBUG) # Force le niveau DEBUG pour l'optimizer.py

    print("\n--- Démarrage du Test de l'Optimizer ---")

    try:
        prod_config_path = Path('config') / 'prod_config.json'
        if not prod_config_path.exists():
            logger.critical(f"FATAL: Le fichier prod_config.json est introuvable à '{prod_config_path}'.")
            sys.exit(1)

        # ConfigManager pour l'Optimizer (ne sera pas modifié durant l'optimisation)
        config_manager_instance_for_optimizer = ConfigManager()
        config_manager_instance_for_optimizer.initialize_dynamic_config(
            template_path=str(prod_config_path),
            output_path=str(Path('output') / 'dynamic_config_optimizer_base_temp.json'),
            config_dir=str(Path('config') / 'strategy')
        )
        logger.info("ConfigManager global initialisé avec succès pour l'Optimizer.")
    except Exception as e:
        logger.critical(f"Erreur critique lors de l'initialisation du ConfigManager global : {e}", exc_info=True)
        sys.exit(1)
        
    # --- Initialisation de l'Optimizer ---
    optimizer = Optimizer(config_manager=config_manager_instance_for_optimizer, backtest_engine_class=BacktestEngine)

    # --- Définition de la stratégie à optimiser et de la période de backtest ---
    strategy_to_optimize_name = "Dynamic Institutional" 

    # Période de backtest pour l'optimisation (courte pour les tests rapides)
    optimization_start_date = datetime(2025, 4, 7, tzinfo=UTC)
    optimization_end_date = datetime(2025, 4, 9, tzinfo=UTC) # 2 semaines de données pour le test initial

    # --- Lancement de l'Optimisation ---
    print(f"\n--- Lancement de l'Optimisation pour '{strategy_to_optimize_name}' ---")
    best_results = optimizer.run_optimization(
        strategy_name_to_optimize=strategy_to_optimize_name,
        backtest_timeframe="M1", # Le timeframe de vos données historiques
        backtest_start_date=optimization_start_date,
        backtest_end_date=optimization_end_date
    )

    print("\n--- Aperçu des Résultats d'Optimisation ---")
    if best_results:
        for result in best_results:
            # --- DÉBUT DE LA CORRECTION DÉFENSIVE ICI ---
            # S'assurer que result['results'] est un dictionnaire valide, sinon utiliser un dictionnaire vide.
            actual_results = result.get('results')
            if not isinstance(actual_results, dict):
                actual_results = {} # Utiliser un dict vide si les résultats sont None ou non-dictionnaire

            # Récupérer les valeurs avec des défauts sûrs pour le formatage
            profit = actual_results.get('total_profit_usd', 0.0)
            net_profit = actual_results.get('net_profit_percent', 0.0)
            drawdown = actual_results.get('max_drawdown_percent', 0.0)
            
            print(f"Paramètres: {result['parameters']}")
            print(f"  Résultats: Profit={profit:.2f}$, Net Profit={net_profit:.2f}%, Drawdown={drawdown:.2f}%")
            # --- FIN DE LA CORRECTION DÉFENSIVE ---

        # Générer les fichiers de configuration optimisés dans le dossier 'output/'
        # Utilisez l'instance de l'Optimizer pour appeler la méthode
        optimizer._generate_optimized_configs(Path('output') / 'optimized_configs')
    else:
        print("Aucun résultat d'optimisation généré ou trouvé.")

    print("\n--- Fin du Test de l'Optimizer ---")