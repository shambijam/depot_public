#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
start_dashboard.py - Script de lancement du Dashboard SNIPER_X
Lance le dashboard web en mode standalone ou intégré avec le bot
"""

import argparse
import logging
import sys
from pathlib import Path
from threading import Thread

# Configurer le logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

# Importer dashboard_server (obligatoire)
try:
    from dashboard_server import run_dashboard, initialize_dashboard
except ImportError as e:
    logger.critical(f"Erreur d'importation dashboard_server: {e}")
    logger.critical("Assurez-vous que tous les modules sont installés:")
    logger.critical("  pip install flask flask-cors flask-socketio python-socketio")
    sys.exit(1)

# Importer les modules bot (optionnel pour mode standalone)
ConfigManager = None
MT5Connector = None

try:
    from core.config_manager import ConfigManager
    from mt5_connector import MT5Connector
except ImportError as e:
    logger.warning(f"⚠️  Modules bot non disponibles: {e}")
    logger.warning("   Mode standalone uniquement")


def launch_standalone_dashboard(host='0.0.0.0', port=5000):
    """Lance le dashboard en mode standalone (pour tests)"""
    logger.info("=" * 60)
    logger.info("🚀 SNIPER_X Dashboard - Mode Standalone")
    logger.info("=" * 60)
    logger.info(f"📊 Dashboard accessible sur: http://{host}:{port}")
    logger.info("⚠️  Mode standalone: Données de démonstration uniquement")
    logger.info("=" * 60)

    initialize_dashboard(bot_mode='DEMO')
    run_dashboard(host=host, port=port, debug=True)


def launch_integrated_dashboard(config_manager, mt5_connector, bot_mode='DEMO', host='0.0.0.0', port=5000):
    """Lance le dashboard intégré avec le bot"""
    logger.info("=" * 60)
    logger.info("🚀 SNIPER_X Dashboard - Mode Intégré")
    logger.info("=" * 60)
    logger.info(f"📊 Dashboard accessible sur: http://{host}:{port}")
    logger.info(f"🤖 Mode Bot: {bot_mode}")
    logger.info("=" * 60)

    initialize_dashboard(config_manager, mt5_connector, bot_mode)

    # Lancer le serveur dans un thread séparé
    dashboard_thread = Thread(
        target=run_dashboard,
        args=(host, port),
        kwargs={'debug': False},
        daemon=True
    )
    dashboard_thread.start()

    logger.info("✅ Dashboard lancé en arrière-plan")
    return dashboard_thread


def main():
    parser = argparse.ArgumentParser(
        description='Lancer le Dashboard SNIPER_X'
    )
    parser.add_argument(
        '--mode',
        choices=['standalone', 'integrated'],
        default='standalone',
        help='Mode de lancement: standalone (test) ou integrated (avec bot)'
    )
    parser.add_argument(
        '--host',
        default='0.0.0.0',
        help='Adresse IP du serveur (défaut: 0.0.0.0)'
    )
    parser.add_argument(
        '--port',
        type=int,
        default=5000,
        help='Port du serveur (défaut: 5000)'
    )
    parser.add_argument(
        '--bot-mode',
        choices=['DEMO', 'LIVE'],
        default='DEMO',
        help='Mode du bot (défaut: DEMO)'
    )

    args = parser.parse_args()

    # Créer les dossiers nécessaires
    Path('dashboard/templates').mkdir(parents=True, exist_ok=True)
    Path('dashboard/static/css').mkdir(parents=True, exist_ok=True)
    Path('dashboard/static/js').mkdir(parents=True, exist_ok=True)

    if args.mode == 'standalone':
        launch_standalone_dashboard(args.host, args.port)
    else:
        # Mode intégré - initialiser le bot
        if ConfigManager is None or MT5Connector is None:
            logger.critical("❌ Mode intégré impossible: modules bot non disponibles")
            logger.critical("   Utilisez --mode standalone ou installez les dépendances du bot")
            sys.exit(1)

        try:
            logger.info("Initialisation du ConfigManager...")
            config_manager = ConfigManager()

            logger.info("Initialisation du MT5Connector...")
            mt5_connector = MT5Connector()

            # Connexion à MT5
            account_details = config_manager.get_mt5_account_credentials(mode=args.bot_mode)
            if account_details and mt5_connector.connect(account_details):
                logger.info(f"✅ Connecté à MT5: {account_details['account_id']}")
            else:
                logger.warning("⚠️  Connexion MT5 échouée - Dashboard en mode limité")

            launch_integrated_dashboard(
                config_manager,
                mt5_connector,
                args.bot_mode,
                args.host,
                args.port
            )

            # Maintenir le programme en vie
            logger.info("Dashboard en cours d'exécution. Appuyez sur Ctrl+C pour arrêter.")
            try:
                while True:
                    import time
                    time.sleep(1)
            except KeyboardInterrupt:
                logger.info("\n👋 Arrêt du dashboard...")
                if mt5_connector and mt5_connector.is_connected:
                    mt5_connector.disconnect()

        except Exception as e:
            logger.critical(f"Erreur fatale: {e}", exc_info=True)
            sys.exit(1)


if __name__ == '__main__':
    main()
