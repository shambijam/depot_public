#!/bin/bash
# Script de lancement rapide du Dashboard SNIPER_X

echo "=========================================="
echo "   SNIPER_X Dashboard - Lancement"
echo "=========================================="
echo ""

# Activer l'environnement virtuel si disponible
if [ -d ".venv" ]; then
    echo "Activation de l'environnement virtuel..."
    source .venv/bin/activate
elif [ -d "venv" ]; then
    echo "Activation de l'environnement virtuel..."
    source venv/bin/activate
fi

# Vérifier les dépendances
echo "Vérification des dépendances..."
pip install -q flask flask-cors flask-socketio python-socketio

echo ""
echo "=========================================="
echo "Lancement du dashboard en mode standalone..."
echo "=========================================="
echo ""
echo "Le dashboard sera accessible sur:"
echo "  - Local:   http://localhost:5000"
echo "  - Réseau:  http://$(hostname -I | awk '{print $1}'):5000"
echo ""
echo "Appuyez sur Ctrl+C pour arrêter"
echo ""

# Lancer le dashboard
python start_dashboard.py --mode standalone --host 0.0.0.0 --port 5000
