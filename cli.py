import argparse
import logging
import sys
import os # Nécessaire pour les variables d'environnement si les modules internes les lisent directement
from pathlib import Path
from dotenv import load_dotenv

# Configurer un logger de base pour cli.py avant l'initialisation complète du logging
# (ceci sera surchargé par setup_production_logging dans main.py)
#logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# Charger les variables d'environnement TÔT pour qu'elles soient disponibles partout
load_dotenv()

# Importer la fonction main depuis main.py pour lancer le bot
# La logique principale du bot réside dans main.py
from main import main as run_main_bot_logic 

# TODO: Importer d'autres fonctions pour les sous-commandes (ex: backtest, report) depuis leurs modules respectifs
# from trading_pipeline.backtester import run_backtest_cli_function
# from reporting.report_generator import generate_report_cli_function

def parse_args() -> argparse.Namespace:
    """
    Parse les arguments de ligne de commande pour la CLI principale du bot SNIPER_X.

    Cette fonction configure un analyseur d'arguments capable de gérer diverses commandes
    pour lancer le bot, effectuer des backtests, générer des rapports, etc.

    Returns:
        argparse.Namespace: Un objet Namespace contenant les arguments parsés.
    """
    parser = argparse.ArgumentParser(
        description="Lanceur de bot de trading institutionnel SNIPER_X.",
        formatter_class=argparse.RawTextHelpFormatter
    )

    # Sous-commandes: permet d'avoir 'cli.py start', 'cli.py backtest', etc.
    subparsers = parser.add_subparsers(dest="command", help="Commande à exécuter")

    # --- Commande 'start': Lancer le bot de trading ---
    start_parser = subparsers.add_parser(
        "start", 
        help="Lance le bot de trading en mode opérationnel (LIVE ou DEMO)."
    )
    start_parser.add_argument(
        "--interval", 
        type=int, 
        help="Intervalle de temps en secondes entre chaque cycle de trading (priorité sur la configuration)."
    )
    start_parser.add_argument(
        "--log-level", 
        type=str, 
        default="INFO",
        choices=["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"],
        help="Définir le niveau de logging pour le bot (priorité sur la configuration)."
    )
    start_parser.add_argument(
        "--dry-run", 
        action="store_true",
        help="Lance le bot en mode simulation (dry run) sans exécuter de trades réels. Applicable aux modes DEMO et LIVE."
    )
    start_parser.add_argument(
        "--mode", 
        type=str, 
        choices=["DEMO", "LIVE"],
        help="Mode d'exécution du bot : DEMO (pour le développement/test) ou LIVE (pour le trading réel)."
    )
    start_parser.add_argument(
        "--config-path",
        type=str,
        help="Chemin vers le fichier de configuration principal (ex: config/prod_config.json). Priorité sur la configuration par défaut."
    )


    # --- TODO: Ajouter d'autres sous-commandes ici (exemples commentés) ---

    # # Commande 'backtest': Lancer un backtest
    # backtest_parser = subparsers.add_parser(
    #     "backtest", help="Lance une simulation de backtest pour une stratégie donnée."
    # )
    # backtest_parser.add_argument("--strategy", type=str, required=True, help="Nom de la stratégie à backtester.")
    # backtest_parser.add_argument("--data", type=str, required=True, help="Chemin vers le fichier de données historiques.")
    # # TODO: Ajouter d'autres arguments pour backtest (ex: date de début/fin, paramètres d'optimisation)

    # # Commande 'report': Générer un rapport
    # report_parser = subparsers.add_parser(
    #     "report", help="Génère divers rapports d'activité et de performance."
    # )
    # report_parser.add_argument("--type", type=str, choices=["daily", "weekly", "audit"], required=True, help="Type de rapport à générer.")
    # report_parser.add_argument("--date", type=str, help="Date spécifique pour le rapport (format YYYY-MM-DD).")
    # # TODO: Ajouter d'autres arguments pour report (ex: chemin de sortie, format)

    # # Commande 'check-config': Vérifier la configuration
    # check_config_parser = subparsers.add_parser(
    #     "check-config", help="Effectue une vérification de la configuration et de l'environnement du bot."
    # )
    # check_config_parser.add_argument("--config-path", type=str, help="Chemin vers le fichier de configuration principal à vérifier.")


    # Si aucune commande n'est fournie, afficher l'aide
    if len(sys.argv) == 1:
        parser.print_help(sys.stderr)
        sys.exit(1)
    
    return parser.parse_args()

def main() -> None:
    """
    Fonction principale de la CLI.
    Elle parse les arguments et dispatch la commande appropriée.
    """
    args = parse_args()

    if args.command == "start":
        logger.info("Lancement du bot de trading SNIPER_X via la commande 'start'...")
        # Passer l'objet args directement à la fonction main du bot (main.py)
        # main.py s'occupera d'interpréter ces arguments et de lancer le cycle.
        run_main_bot_logic(args=args)

    # TODO: Ajouter la logique de dispatch pour d'autres commandes ici
    # elif args.command == "backtest":
    #     logger.info(f"Lancement du backtest pour la stratégie {args.strategy}...")
    #     # run_backtest_cli_function(args)
    #     sys.exit(0) # Exit après le backtest
    # elif args.command == "report":
    #     logger.info(f"Génération du rapport de type {args.type}...")
    #     # generate_report_cli_function(args)
    #     sys.exit(0) # Exit après la génération du rapport
    # elif args.command == "check-config":
    #     logger.info("Vérification de la configuration demandée...")
    #     # La logique de vérification est déjà dans main.py (verify_environment_and_config)
    #     # On peut appeler une version légère de main.py ou refactoriser la vérification.
    #     # Pour l'instant, on lance main.py en dry-run pour qu'il fasse les checks.
    #     args.dry_run = True # Force le dry-run pour juste la vérification
    #     run_main_bot_logic(args=args)
    #     sys.exit(0)
    else:
        logger.error(f"Commande inconnue ou non implémentée : {args.command}")
        sys.exit(1)

if __name__ == "__main__":
    main()