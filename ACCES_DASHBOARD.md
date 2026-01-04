# 🌐 Guide d'Accès au Dashboard SNIPER_X

## 📱 Vos URLs d'accès

### Depuis cet ordinateur (local)
```
http://localhost:5000
http://127.0.0.1:5000
```

### Depuis un autre appareil sur le même réseau Wi-Fi
```
http://192.168.1.108:5000
```

### Depuis votre téléphone/tablette (même Wi-Fi)
1. Assurez-vous que votre téléphone est sur le **même Wi-Fi**
2. Ouvrez le navigateur sur votre téléphone
3. Tapez : `http://192.168.1.108:5000`

## 🚀 Étapes complètes pour accéder

### Étape 1 : Lancer le dashboard
Dans votre terminal :
```bash
./launch_dashboard.sh
```

ou
```bash
python start_dashboard.py --mode standalone
```

Attendez de voir :
```
🚀 SNIPER_X Dashboard - Mode Standalone
📊 Dashboard accessible sur: http://0.0.0.0:5000
```

### Étape 2 : Ouvrir dans le navigateur

**Option A - Sur cet ordinateur :**
- Ouvrez Chrome/Firefox/Edge
- Tapez : `localhost:5000`
- Appuyez sur Entrée

**Option B - Sur un autre appareil (même Wi-Fi) :**
- Ouvrez le navigateur sur votre téléphone/tablette
- Tapez : `192.168.1.108:5000`
- Appuyez sur Entrée

### Étape 3 : Vérifier que ça fonctionne

Vous devriez voir :
- ✅ Les images sniper en haut de la page
- ✅ Le titre "SNIPER_X"
- ✅ La barre de statut
- ✅ Les cartes de performance
- ✅ Le graphique

## 🔍 Dépannage

### Le site ne charge pas ?

1. **Vérifiez que le serveur tourne**
   - Le terminal doit afficher "Running on http://0.0.0.0:5000"
   - Pas de message d'erreur

2. **Vérifiez votre URL**
   - Local : `http://localhost:5000` (pas `https://`)
   - Réseau : `http://192.168.1.108:5000`

3. **Vérifiez votre firewall**
   ```bash
   # Autoriser le port 5000
   sudo ufw allow 5000
   ```

4. **Testez depuis cet ordinateur d'abord**
   ```bash
   curl http://localhost:5000
   ```

### Erreur "Connection refused" ?

Le serveur n'est pas lancé. Lancez-le :
```bash
python start_dashboard.py --mode standalone
```

### Les images ne s'affichent pas ?

Vérifiez que le dossier sniper_x contient bien :
```bash
ls -la sniper_x/
```

Vous devriez voir :
- sniper_left.png
- sniper_right.png

## 📡 Accès depuis Internet (Avancé - Non recommandé)

Si vous voulez vraiment accéder depuis n'importe où :

### Avec Ngrok (Solution simple et sécurisée)

1. **Installer Ngrok**
   ```bash
   # Télécharger depuis https://ngrok.com/download
   # Ou :
   wget https://bin.equinox.io/c/bNyj1mQVY4c/ngrok-v3-stable-linux-amd64.tgz
   tar xvzf ngrok-v3-stable-linux-amd64.tgz
   ```

2. **Lancer le dashboard**
   ```bash
   python start_dashboard.py --mode standalone
   ```

3. **Dans un autre terminal, lancer Ngrok**
   ```bash
   ./ngrok http 5000
   ```

4. **Ngrok vous donnera une URL publique**
   ```
   Forwarding: https://abc123.ngrok.io -> http://localhost:5000
   ```

5. **Accédez depuis n'importe où**
   - Ouvrez cette URL sur votre téléphone (même en 4G/5G)
   - Partagez-la avec d'autres (temporairement)

⚠️ **Note** : La version gratuite de Ngrok change l'URL à chaque redémarrage.

## 🔐 Sécurité

### Recommandations

1. **N'exposez PAS directement le dashboard sur Internet** sans authentification
2. **Utilisez localhost** si vous n'avez besoin que d'un accès local
3. **Utilisez Ngrok** pour un accès temporaire sécurisé
4. **Limitez l'accès au réseau local** pour une utilisation quotidienne

### Pour restreindre l'accès à localhost uniquement

```bash
python start_dashboard.py --mode standalone --host 127.0.0.1
```

Ainsi, seul cet ordinateur pourra y accéder.

## 💡 Conseils

- **Bookmark** l'URL sur votre téléphone pour un accès rapide
- **Testez toujours en local** avant d'essayer depuis un autre appareil
- **Gardez le terminal ouvert** tant que vous utilisez le dashboard
- **Utilisez Ctrl+C** dans le terminal pour arrêter le serveur

## 📞 Checklist rapide

- [ ] Le serveur dashboard est lancé (terminal ouvert)
- [ ] Pas de message d'erreur dans le terminal
- [ ] URL correcte : `http://` (pas `https://`)
- [ ] Port 5000 dans l'URL
- [ ] Même réseau Wi-Fi (si accès depuis téléphone)
- [ ] Firewall autorise le port 5000

---

**Profitez de votre dashboard !** 🚀
