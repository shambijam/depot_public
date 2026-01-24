# 📊 Dashboard SNIPER_X - Guide d'Utilisation

## 🎯 Description

Dashboard web en temps réel pour monitorer votre bot de trading SNIPER_X. Interface moderne et responsive entièrement en français avec vos images personnalisées.

## ✨ Fonctionnalités

### 📈 Performance & Compte
- **Balance** et **Équité** en temps réel
- **Profit/Perte** avec code couleur (vert/rouge)
- **Nombre de trades** exécutés aujourd'hui
- **Informations de marge** (utilisée, libre, niveau)
- **Graphique d'équité** en temps réel

### 💼 Positions Ouvertes
- Vue détaillée de toutes vos positions
- Ticket, symbole, type (ACHAT/VENTE)
- Prix d'ouverture, prix actuel
- Stop Loss (SL) et Take Profit (TP)
- Profit en temps réel

### 🎯 Signaux & Stratégies
- Affichage des signaux actifs
- Stratégie utilisée et score
- Type de signal (BUY/SELL)
- Timeframe et prix

### 📝 Logs en Temps Réel
- Logs du bot en direct
- Filtrage par niveau (INFO, WARNING, ERROR)
- Boutons **Pause** et **Effacer**
- Auto-scroll

## 🚀 Installation

### 1. Installer les dépendances

```bash
pip install flask flask-cors flask-socketio python-socketio
```

### 2. Vérifier la structure des fichiers

```
sniper_x_dev/
├── dashboard_server.py          # Serveur Flask
├── start_dashboard.py           # Script de lancement
├── sniper_x/                    # Vos images
│   ├── sniper_left.png
│   └── sniper_right.png
└── dashboard/
    ├── templates/
    │   └── dashboard.html       # Interface HTML
    └── static/
        ├── css/
        │   └── dashboard.css    # Styles
        └── js/
            └── dashboard.js     # Logique JavaScript
```

## 📱 Utilisation

### Mode Standalone (Tests)

Lancez le dashboard seul pour tester l'interface :

```bash
python start_dashboard.py --mode standalone
```

Accédez au dashboard sur : **http://localhost:5000**

### Mode Intégré (Avec le Bot)

#### Option 1 : Lancement séparé

Terminal 1 - Le bot :
```bash
python cli.py --mode DEMO
```

Terminal 2 - Le dashboard :
```bash
python start_dashboard.py --mode integrated --bot-mode DEMO
```

#### Option 2 : Modifier main.py (Recommandé)

Ajoutez ces lignes dans votre `main.py` après l'initialisation des modules :

```python
# Import du dashboard
from start_dashboard import launch_integrated_dashboard

# Dans la fonction main(), après la création de config_manager et mt5_connector:
dashboard_thread = launch_integrated_dashboard(
    config_manager=config_manager,
    mt5_connector=mt5_connector,
    bot_mode=bot_mode,
    host='0.0.0.0',
    port=5000
)
```

Puis lancez normalement :
```bash
python cli.py --mode DEMO
```

## 🌐 Accès au Dashboard

### Depuis votre machine
- **URL** : http://localhost:5000

### Depuis un autre appareil sur le réseau
1. Trouvez votre IP locale :
   ```bash
   # Linux/Mac
   ip addr show
   # ou
   ifconfig
   ```

2. Accédez depuis un autre appareil :
   - **URL** : http://VOTRE_IP:5000
   - Exemple : http://192.168.1.100:5000

### Changer le port

```bash
python start_dashboard.py --port 8080
```

## 🎨 Personnalisation

### Modifier les couleurs

Éditez `dashboard/static/css/dashboard.css` :

```css
:root {
    --primary-color: #1e40af;      /* Couleur principale */
    --success-color: #10b981;      /* Vert pour profits */
    --danger-color: #ef4444;       /* Rouge pour pertes */
    --dark-bg: #0f172a;            /* Fond sombre */
}
```

### Modifier le nombre de points sur le graphique

Éditez `dashboard/static/js/dashboard.js` :

```javascript
let maxDataPoints = 50; // Changez cette valeur
```

### Intervalle de mise à jour

Par défaut, le dashboard se met à jour toutes les 2 secondes.

Pour modifier, éditez `dashboard_server.py` :

```python
time.sleep(2)  # Ligne ~177 - Changer la valeur
```

## 🔧 Intégration avec le Bot

Le dashboard récupère automatiquement :

- **Compte MT5** : via `mt5_connector.get_account_info()`
- **Positions** : via `mt5_connector.get_positions()`
- **Balance, Équité, Profit** : calculés en temps réel
- **Logs** : ajoutés via `data_collector.add_log()`

### Ajouter des signaux

Dans votre code de stratégie :

```python
# Dans dashboard_server.py, ajoutez après l'initialisation:
from dashboard_server import data_collector

# Quand un signal est détecté:
if data_collector:
    with data_lock:
        bot_data['signals'].append({
            'symbol': 'XAUUSD',
            'type': 'BUY',
            'strategy': 'scalping',
            'score': 85,
            'timeframe': 'M5',
            'price': 2050.50
        })
```

### Ajouter des logs personnalisés

```python
from dashboard_server import data_collector

if data_collector:
    data_collector.add_log("Trade exécuté avec succès", "SUCCESS")
    data_collector.add_log("Attention: volatilité élevée", "WARNING")
    data_collector.add_log("Erreur de connexion", "ERROR")
```

## 🐛 Dépannage

### Le dashboard ne se lance pas

1. Vérifiez les dépendances :
   ```bash
   pip install flask flask-cors flask-socketio python-socketio
   ```

2. Vérifiez que les dossiers existent :
   ```bash
   mkdir -p dashboard/templates dashboard/static/css dashboard/static/js
   ```

### Les images ne s'affichent pas

Vérifiez que le dossier `sniper_x/` contient bien :
- `sniper_left.png`
- `sniper_right.png`

### Pas de données affichées

1. Vérifiez que MT5 est connecté (indicateur en haut)
2. Vérifiez les logs dans la console
3. Ouvrez la console du navigateur (F12) pour voir les erreurs

### Le graphique ne se met pas à jour

1. Vérifiez la connexion WebSocket dans la console (F12)
2. Rechargez la page (Ctrl+F5)

## 📊 Captures d'écran

Le dashboard affiche :
- ✅ **En-tête** avec vos images sniper
- ✅ **Barre de statut** (Bot, Mode, MT5, Uptime)
- ✅ **Cartes de performance** (Balance, Équité, P&L, Trades)
- ✅ **Tableau des positions** avec toutes les informations
- ✅ **Section signaux** avec stratégies actives
- ✅ **Logs en temps réel** avec code couleur
- ✅ **Graphique d'équité** animé
- ✅ **Footer** avec vos images

## 🔐 Sécurité

**⚠️ IMPORTANT** : Par défaut, le dashboard est accessible depuis n'importe quelle IP (`0.0.0.0`).

### Restreindre l'accès à localhost uniquement

```bash
python start_dashboard.py --host 127.0.0.1
```

### Ajouter une authentification (optionnel)

Pour plus de sécurité, vous pouvez ajouter une authentification dans `dashboard_server.py`.

## 📝 Notes

- Le dashboard fonctionne sur **tous les navigateurs modernes**
- Interface **100% responsive** (mobile, tablette, desktop)
- Mise à jour en **temps réel** via WebSocket
- **Pas besoin de rafraîchir** la page
- Toutes les données en **français**

## 🚀 Prochaines améliorations possibles

- [ ] Historique des trades
- [ ] Notifications push
- [ ] Export CSV/PDF des rapports
- [ ] Statistiques avancées
- [ ] Contrôle du bot depuis le dashboard
- [ ] Mode sombre/clair
- [ ] Alertes configurables

## 📞 Support

Pour toute question ou problème, vérifiez :
1. Les logs du serveur dans le terminal
2. La console du navigateur (F12)
3. Les fichiers de logs du bot

---

**Développé avec ❤️ pour SNIPER_X Trading Bot**
