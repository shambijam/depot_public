#!/bin/bash
# reload_config.sh - Script helper pour recharger la configuration du bot sans redémarrage
#
# Usage:
#   ./reload_config.sh              # Recharge automatiquement (détecte le PID)
#   ./reload_config.sh <PID>        # Recharge pour un PID spécifique

set -e

echo "🔄 Script de rechargement config SNIPER_X"
echo "=========================================="

# Détection du PID du bot
if [ -n "$1" ]; then
    BOT_PID="$1"
    echo "📌 PID fourni manuellement: $BOT_PID"
else
    # Recherche automatique du PID (python cli.py ou python run_bot.py)
    BOT_PID=$(pgrep -f "python.*cli.py|python.*run_bot.py" | head -n 1)

    if [ -z "$BOT_PID" ]; then
        echo "❌ Erreur: Impossible de trouver le processus du bot"
        echo "💡 Lancez le bot ou spécifiez le PID: ./reload_config.sh <PID>"
        exit 1
    fi

    echo "✅ Bot détecté automatiquement (PID: $BOT_PID)"
fi

# Vérification que le processus existe
if ! kill -0 "$BOT_PID" 2>/dev/null; then
    echo "❌ Erreur: Le processus PID $BOT_PID n'existe pas ou n'est pas accessible"
    exit 1
fi

# Envoi du signal SIGUSR1
echo "📤 Envoi du signal de rechargement (SIGUSR1) au PID $BOT_PID..."
kill -SIGUSR1 "$BOT_PID"

if [ $? -eq 0 ]; then
    echo "✅ Signal envoyé avec succès !"
    echo "📊 Vérifiez les logs du bot pour confirmer le rechargement:"
    echo "   tail -f logs/sniper_x_main.log | grep HOT-RELOAD"
    echo ""
    echo "🎯 Configs rechargées:"
    echo "   - prod_config.json"
    echo "   - config_trade_scalping.json"
    echo "   - config_trade_liquidity.json"
    echo "   - XAUUSD.json / EURUSD.json / GBPUSD.json"
    echo "   - phase_observer_config.json"
    echo "   - telegram_config.json"
else
    echo "❌ Erreur lors de l'envoi du signal"
    exit 1
fi
