# Contenu pour le nouveau fichier : utils/logger_setup.py

import logging
import sys
from pathlib import Path
from logging.handlers import RotatingFileHandler

# NOTE: L'import de ConfigManager est nécessaire pour accéder aux chemins dynamiques.
# Il est préférable de le passer en argument pour éviter les imports circulaires.
# Pour l'instant, nous gardons l'import local pour suivre la structure existante.


def setup_production_logging(log_level: str = "INFO", console_level: str = "WARNING") -> None:
    """
    Initialise un logging de qualité institutionnelle pour le bot.
    Cette fonction est la source unique de vérité pour la configuration du logging.

    Args:
        log_level: Niveau pour le fichier log (DEBUG, INFO, WARNING, ERROR, CRITICAL)
        console_level: Niveau pour la console (défaut: WARNING pour affichage propre)

    🎯 (05 JAN 2026): console_level=WARNING pour épurer la console
    Le dashboard affiche les infos importantes, la console ne montre que warnings/errors
    """
    try:
        from config_manager.config_manager import ConfigManager
        config_manager_instance = ConfigManager()
        log_dir_str = config_manager_instance.get("paths.logs", "logs/")
        log_file_name_str = config_manager_instance.get("paths.run_bot_log_file_name", "sniper_x_main.log")
    except Exception:
        # Fallback si ConfigManager n'est pas disponible (ex: tests unitaires)
        log_dir_str = "logs"
        log_file_name_str = "sniper_x_main.log"

    log_dir = Path(log_dir_str)
    log_dir.mkdir(parents=True, exist_ok=True)
    log_file_path = log_dir / log_file_name_str

    root_logger = logging.getLogger()

    # Valider et définir le niveau de log
    log_level_upper = log_level.upper()
    if log_level_upper not in ["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"]:
        log_level_upper = "INFO"
    
    root_logger.setLevel(getattr(logging, log_level_upper))

    # Nettoyer les handlers précédents pour éviter la duplication des logs
    if root_logger.hasHandlers():
        for handler in list(root_logger.handlers):
            root_logger.removeHandler(handler)

    # Configurer le handler pour le fichier log avec rotation
    # ✅ FIX (17 DEC): RotatingFileHandler pour éviter disque plein
    # Max 50 MB par fichier, 5 fichiers gardés = 250 MB total max
    file_handler = RotatingFileHandler(
        log_file_path,
        mode="a",
        maxBytes=50 * 1024 * 1024,  # 50 MB
        backupCount=5,               # Garder 5 fichiers rotationnés
        encoding="utf-8"
    )
    file_formatter = logging.Formatter(
        "%(asctime)s - %(name)s - [%(levelname)s] - %(message)s"
    )
    file_handler.setFormatter(file_formatter)
    root_logger.addHandler(file_handler)

    # 🎯 (05 JAN 2026): Console affiche uniquement WARNING+ pour épurer l'affichage
    # Le dashboard gère l'affichage des infos importantes (scores, trades)
    # Les logs détaillés (INFO/DEBUG) vont dans le fichier uniquement
    console_handler = logging.StreamHandler(sys.stdout)
    console_formatter = logging.Formatter("[%(levelname)s] - %(message)s")
    console_handler.setFormatter(console_formatter)

    # Définir niveau console séparément du fichier
    console_level_upper = console_level.upper()
    if console_level_upper not in ["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"]:
        console_level_upper = "WARNING"
    console_handler.setLevel(getattr(logging, console_level_upper))

    root_logger.addHandler(console_handler)

    logging.info(f"Logging de production initialisé. Niveau: {log_level_upper}. Fichier: {log_file_path}")

    # Empêcher les loggers des modules spécifiques de propager au logger racine
    # car ils ont leurs propres handlers dédiés.
    modules_to_isolate = [
        "Mecano", "PhaseObserver", "ai_core.ai_decision", 
        "config_manager.config_manager", "trader.trade_executor", "mt5_connector"
    ]
    for module_name in modules_to_isolate:
        logging.getLogger(module_name).propagate = False