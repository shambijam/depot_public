#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
dashboard_server.py - Serveur Web Dashboard pour SNIPER_X Bot
Fournit une interface web en temps réel pour monitorer le bot de trading
"""

import json
import logging
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from threading import Thread, Lock
from flask import Flask, render_template, jsonify, send_from_directory
from flask_cors import CORS
from flask_socketio import SocketIO, emit
import time

# Importer les modules du bot (optionnel pour mode standalone)
ConfigManager = None
MT5Connector = None
TradesStatsAnalyzer = None

try:
    from core.config_manager import ConfigManager
    from mt5_connector import MT5Connector
except ImportError as e:
    print(f"⚠️  Avertissement: Modules bot non disponibles ({e})")
    print("   Le dashboard fonctionnera en mode standalone uniquement")

try:
    from core.stats_analyzer import TradesStatsAnalyzer
except ImportError as e:
    print(f"⚠️  Avertissement: Module stats_analyzer non disponible ({e})")
    print("   Les statistiques de trading ne seront pas disponibles")

# Configuration Flask - FIX (17 JAN 2026): Chemins absolus pour compatibilité Windows/Linux
BASE_DIR = Path(__file__).resolve().parent
TEMPLATE_DIR = BASE_DIR / 'dashboard' / 'templates'
STATIC_DIR = BASE_DIR / 'dashboard' / 'static'

app = Flask(__name__,
            template_folder=str(TEMPLATE_DIR),
            static_folder=str(STATIC_DIR))
app.config['SECRET_KEY'] = os.getenv('DASHBOARD_SECRET_KEY', 'sniper_x_dashboard_2026')
CORS(app)
socketio = SocketIO(app, cors_allowed_origins="*", async_mode='threading')

# Logger
logger = logging.getLogger(__name__)

# Variables globales pour stocker les données du bot
bot_data = {
    'status': 'stopped',
    'mode': 'DEMO',
    'balance': 0.0,
    'equity': 0.0,
    'profit_loss': 0.0,
    'positions': [],
    'signals': [],
    'logs': [],
    'trades_today': 0,
    'uptime': 0,
    'last_update': None,
    'mt5_connected': False
}
data_lock = Lock()

# Gestionnaire de configuration
config_manager = None
mt5_connector = None


class DashboardDataCollector:
    """Collecteur de données pour le dashboard"""

    def __init__(self, config_mgr, mt5_conn):
        self.config_manager = config_mgr
        self.mt5_connector = mt5_conn
        self.start_time = datetime.now(timezone.utc)
        self.logs_buffer = []
        self.max_logs = 100

        # Analyseur de statistiques de trading
        if TradesStatsAnalyzer:
            self.stats_analyzer = TradesStatsAnalyzer()
        else:
            self.stats_analyzer = None

    def get_account_data(self):
        """Récupère les données du compte MT5"""
        if not self.mt5_connector or not self.mt5_connector.is_connected:
            return {
                'balance': 0.0,
                'equity': 0.0,
                'profit_loss': 0.0,
                'mt5_connected': False
            }

        try:
            account_info = self.mt5_connector.get_account_info()
            if account_info:
                return {
                    'balance': float(account_info.balance),
                    'equity': float(account_info.equity),
                    'profit_loss': float(account_info.profit),
                    'margin': float(account_info.margin),
                    'free_margin': float(account_info.margin_free),
                    'margin_level': float(account_info.margin_level) if account_info.margin > 0 else 0,
                    'mt5_connected': True
                }
        except Exception as e:
            logger.error(f"Erreur récupération données compte: {e}")

        return {
            'balance': 0.0,
            'equity': 0.0,
            'profit_loss': 0.0,
            'mt5_connected': False
        }

    def get_positions(self):
        """Récupère les positions ouvertes"""
        if not self.mt5_connector or not self.mt5_connector.is_connected:
            return []

        try:
            positions = self.mt5_connector.get_positions()
            if positions:
                return [{
                    'ticket': pos.ticket,
                    'symbol': pos.symbol,
                    'type': 'ACHAT' if pos.type == 0 else 'VENTE',
                    'volume': float(pos.volume),
                    'price_open': float(pos.price_open),
                    'price_current': float(pos.price_current),
                    'profit': float(pos.profit),
                    'sl': float(pos.sl) if pos.sl > 0 else None,
                    'tp': float(pos.tp) if pos.tp > 0 else None,
                    'time': datetime.fromtimestamp(pos.time).strftime('%Y-%m-%d %H:%M:%S')
                } for pos in positions]
        except Exception as e:
            logger.error(f"Erreur récupération positions: {e}")

        return []

    def get_uptime(self):
        """Calcule le temps de fonctionnement"""
        delta = datetime.now(timezone.utc) - self.start_time
        hours = delta.seconds // 3600
        minutes = (delta.seconds % 3600) // 60
        return f"{delta.days}j {hours}h {minutes}m"

    def add_log(self, message, level='INFO'):
        """Ajoute un log au buffer"""
        log_entry = {
            'timestamp': datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S'),
            'level': level,
            'message': message
        }
        self.logs_buffer.append(log_entry)
        if len(self.logs_buffer) > self.max_logs:
            self.logs_buffer.pop(0)
        return log_entry

    def get_trading_stats(self, days=7):
        """Récupère les statistiques de trading"""
        if not self.stats_analyzer:
            return self._get_empty_stats()

        try:
            return self.stats_analyzer.get_all_stats(days_equity=days)
        except Exception as e:
            logger.error(f"Erreur récupération stats trading: {e}")
            return self._get_empty_stats()

    def _get_empty_stats(self):
        """Retourne des stats vides"""
        return {
            'global': {
                'total_trades': 0,
                'wins': 0,
                'losses': 0,
                'be': 0,
                'win_rate': 0.0,
                'avg_rr': 0.0,
                'biggest_win_pips': 0.0,
                'biggest_win_usd': 0.0,
                'biggest_loss_pips': 0.0,
                'biggest_loss_usd': 0.0,
                'total_pnl_usd': 0.0
            },
            'by_symbol': {},
            'equity_evolution': {'dates': [], 'equity': [], 'cumulative_pnl': []},
            'hourly_performance': {},
            'has_data': False
        }

    def update_bot_data(self):
        """Met à jour toutes les données du bot"""
        global bot_data

        try:
            account_data = self.get_account_data()
            positions = self.get_positions()
            trading_stats = self.get_trading_stats()

            with data_lock:
                bot_data.update({
                    'balance': account_data['balance'],
                    'equity': account_data['equity'],
                    'profit_loss': account_data['profit_loss'],
                    'margin': account_data.get('margin', 0),
                    'free_margin': account_data.get('free_margin', 0),
                    'margin_level': account_data.get('margin_level', 0),
                    'positions': positions,
                    'uptime': self.get_uptime(),
                    'last_update': datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S'),
                    'mt5_connected': account_data['mt5_connected'],
                    'logs': self.logs_buffer[-50:],  # Derniers 50 logs
                    'trading_stats': trading_stats  # NOUVEAU: Stats de trading
                })

        except Exception as e:
            logger.error(f"Erreur mise à jour données bot: {e}")


# Instance du collecteur
data_collector = None


def background_data_updater():
    """Thread qui met à jour les données en continu"""
    global data_collector

    while True:
        try:
            if data_collector:
                data_collector.update_bot_data()

                # Émettre les données via WebSocket
                with data_lock:
                    socketio.emit('bot_update', bot_data, namespace='/')

        except Exception as e:
            logger.error(f"Erreur dans background_data_updater: {e}")

        time.sleep(2)  # Mise à jour toutes les 2 secondes


# === Routes API ===

@app.route('/')
def index():
    """Page principale du dashboard"""
    return render_template('dashboard.html')


@app.route('/api/status')
def api_status():
    """Retourne le status actuel du bot"""
    with data_lock:
        return jsonify(bot_data)


@app.route('/api/account')
def api_account():
    """Retourne les informations du compte"""
    with data_lock:
        return jsonify({
            'balance': bot_data['balance'],
            'equity': bot_data['equity'],
            'profit_loss': bot_data['profit_loss'],
            'margin': bot_data.get('margin', 0),
            'free_margin': bot_data.get('free_margin', 0),
            'margin_level': bot_data.get('margin_level', 0),
            'mt5_connected': bot_data['mt5_connected']
        })


@app.route('/api/positions')
def api_positions():
    """Retourne les positions ouvertes"""
    with data_lock:
        return jsonify(bot_data['positions'])


@app.route('/api/logs')
def api_logs():
    """Retourne les logs récents"""
    with data_lock:
        return jsonify(bot_data['logs'])


@app.route('/api/signals')
def api_signals():
    """Retourne les signaux actifs"""
    with data_lock:
        return jsonify(bot_data['signals'])


@app.route('/api/trading-stats')
def api_trading_stats():
    """Retourne les statistiques de trading complètes"""
    from flask import request

    days = request.args.get('days', default=7, type=int)

    if data_collector:
        stats = data_collector.get_trading_stats(days=days)
        return jsonify(stats)

    return jsonify({'error': 'Stats analyzer not available', 'has_data': False}), 503


@app.route('/images/<path:filename>')
def serve_image(filename):
    """Sert les images du dossier sniper_x"""
    return send_from_directory('sniper_x', filename)


# === WebSocket Events ===

@socketio.on('connect')
def handle_connect():
    """Gestion de la connexion WebSocket"""
    logger.info("Client connecté au dashboard")
    with data_lock:
        emit('bot_update', bot_data)


@socketio.on('disconnect')
def handle_disconnect():
    """Gestion de la déconnexion WebSocket"""
    logger.info("Client déconnecté du dashboard")


@socketio.on('request_update')
def handle_request_update():
    """Mise à jour forcée à la demande"""
    if data_collector:
        data_collector.update_bot_data()
    with data_lock:
        emit('bot_update', bot_data)


def initialize_dashboard(config_mgr=None, mt5_conn=None, bot_mode='DEMO'):
    """Initialise le dashboard avec les connexions au bot"""
    global config_manager, mt5_connector, data_collector, bot_data

    config_manager = config_mgr
    mt5_connector = mt5_conn

    # Mettre à jour le mode
    with data_lock:
        bot_data['mode'] = bot_mode
        bot_data['status'] = 'running'

    # Créer le collecteur de données
    if config_manager and mt5_connector:
        data_collector = DashboardDataCollector(config_manager, mt5_connector)
        data_collector.add_log("Dashboard démarré", "INFO")

    # Lancer le thread de mise à jour
    updater_thread = Thread(target=background_data_updater, daemon=True)
    updater_thread.start()

    logger.info("Dashboard initialisé et prêt")


def run_dashboard(host='0.0.0.0', port=5000, debug=False):
    """Lance le serveur dashboard"""
    logger.info(f"Démarrage du serveur dashboard sur http://{host}:{port}")
    socketio.run(app, host=host, port=port, debug=debug, allow_unsafe_werkzeug=True)


if __name__ == '__main__':
    # Configuration du logging
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
    )

    # Créer les dossiers nécessaires
    Path('dashboard/templates').mkdir(parents=True, exist_ok=True)
    Path('dashboard/static/css').mkdir(parents=True, exist_ok=True)
    Path('dashboard/static/js').mkdir(parents=True, exist_ok=True)

    # Lancer en mode standalone (pour tests)
    initialize_dashboard(bot_mode='DEMO')
    run_dashboard(debug=True)
