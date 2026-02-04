# Configuration Telegram - SNIPER_X Bot Controller

## Etape 1 : Creer votre Bot Telegram

1. Ouvrez Telegram et cherchez **@BotFather**
2. Envoyez la commande `/newbot`
3. Suivez les instructions :
   - Donnez un nom a votre bot (ex: "SNIPER_X Trading")
   - Donnez un username (ex: "sniper_x_trading_bot")
4. **BotFather vous donnera un TOKEN** - Copiez-le !

Exemple de token : `7123456789:AAHxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx`

## Etape 2 : Obtenir votre Chat ID

1. Ouvrez Telegram et cherchez **@userinfobot**
2. Envoyez `/start`
3. Le bot vous donnera votre **Chat ID** (un nombre)

Exemple : `123456789`

## Etape 3 : Configurer SNIPER_X

Editez le fichier `config/telegram_config.json` :

```json
{
    "telegram": {
        "enabled": true,
        "bot_token": "7123456789:AAHxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx",
        "authorized_chat_ids": [
            "123456789"
        ],
        ...
    }
}
```

Remplacez :
- `VOTRE_BOT_TOKEN_ICI` par votre token BotFather
- `VOTRE_CHAT_ID_ICI` par votre Chat ID

## Etape 4 : Installer la dependance

```bash
pip install python-telegram-bot
```

## Etape 5 : Tester

1. Lancez votre bot SNIPER_X
2. Ouvrez Telegram et envoyez `/start` a votre bot
3. Vous devriez recevoir le menu d'aide

## Commandes Disponibles

| Commande | Description |
|----------|-------------|
| `/status` | Voir le statut du bot |
| `/balance` | Voir le solde du compte |
| `/positions` | Voir les positions ouvertes |
| `/summary` | Resume complet |
| `/stop` | Arreter le bot |
| `/start_bot` | Demarrer le bot |
| `/close_all` | Fermer toutes les positions |
| `/help` | Afficher l'aide |
| `/ping` | Test de connexion |

## Securite

- Seuls les `authorized_chat_ids` peuvent controler le bot
- Pour ajouter plusieurs utilisateurs :

```json
"authorized_chat_ids": [
    "123456789",
    "987654321"
]
```

## Integration dans le code

Le bot Telegram est demarre automatiquement si `enabled: true`.

Pour l'integrer manuellement dans `main.py` :

```python
from core.telegram_bot import create_telegram_controller

# Apres initialisation de config_manager et mt5_connector :
telegram_controller = create_telegram_controller(
    config_manager=config_manager,
    mt5_connector=mt5_connector,
    logger=logger
)

if telegram_controller:
    telegram_controller.start()

    # Definir les callbacks pour stop/start
    telegram_controller.set_callbacks(
        stop_callback=lambda: stop_bot(),
        start_callback=lambda: start_bot()
    )
```

## Envoyer des notifications depuis le code

```python
# Notification synchrone (depuis code non-async)
telegram_controller.send_message_sync("Trade execute : BUY USDJPY 0.5 lots")

# Notification async
await telegram_controller.send_message("Trade cloture : +$50")
```

## Depannage

### "python-telegram-bot non installe"
```bash
pip install python-telegram-bot
```

### "Acces refuse"
Verifiez que votre Chat ID est dans `authorized_chat_ids`

### Le bot ne repond pas
1. Verifiez que `enabled: true`
2. Verifiez le token
3. Verifiez les logs

---

**Configuration terminee !**
