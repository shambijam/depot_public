# Analyse Processus de Surveillance Trailing Stop

## ✅ Résumé : Le Processus EXISTE et EST DÉMARRÉ

### Question
> Quel est le processus censé activer le trailing ? Y a-t-il un processus qui surveille le PnL en temps réel ?

### Réponse
**OUI !** ✅ Un thread dédié surveille en temps réel toutes les 2 secondes.

---

## 🔍 Architecture du Système de Trailing

### 1. Thread de Surveillance (`trailing_stop_monitor_thread`)

**Fichier** : `run_bot.py` lignes 2738-2838

**Rôle** : Surveille TOUTES les positions ouvertes en temps réel

**Fréquence** : Toutes les **2 secondes** (configurable)

**Configuration** :
```python
# Ligne 3024-3026
trailing_monitor_interval = config_manager.get(
    "entry_rules.scalping.burst_scalping.trailing.step.update_interval_sec",
    2.0  # 2 secondes par défaut
)
```

**Démarrage** : Ligne 3040
```python
trailing_thread.start()  # ✅ DÉMARRÉ au lancement du bot
```

---

### 2. Processus Complet (Étape par Étape)

#### ÉTAPE 1 : Récupération Positions (toutes les 2s)
```python
# Ligne 2778
positions = mt5_connector.get_open_positions()
```

**Log attendu** :
```
📊 [TRAILING_MONITOR] Positions récupérées: 5
```

#### ÉTAPE 2 : Extraction Basket IDs
```python
# Ligne 2791-2799
basket_ids = set()
for pos in positions:
    comment = pos.get("comment", "")
    match = BASKET_PATTERN.search(comment)  # Pattern: bs_abc12345
    if match:
        basket_ids.add(match.group(1))
```

**Log attendu** :
```
🎯 [TRAILING_MONITOR] Baskets détectés: {'abc12345', 'def67890'}
```

**⚠️ POINT CRITIQUE** : Si commentaire != `bs_<8 hex chars>`, basket NON détecté !

#### ÉTAPE 3 : Appel Update Trailing pour Chaque Basket
```python
# Ligne 2816-2820
result = trade_executor.update_basket_sltp_dynamically(
    basket_id=basket_id,
    reason="realtime_monitor",
    force_refresh=False,
)
```

**Log attendu** :
```
🔥 [DEBUG_TRAILING] DÉBUT update_basket_sltp_dynamically
```

#### ÉTAPE 4 : Calcul PnL du Basket
**Fichier** : `trader/sltp.py` fonction `_resolve_basket_context_for_sltp` (ligne 828)

**Avec fallback MT5** (lignes 979-1045) :
```python
# Pour chaque position du basket
pip_value = 10.0 * volume  # XAUUSD: 1 pip = 10$ par lot
pnl_pips = profit / pip_value
total_pnl_pips = sum(pnl_pips)
```

**Log attendu** :
```
[BASKET_CTX] MT5_FALLBACK | abc12345 | XAUUSD BUY | 5 pos | 25.3 pips
```

#### ÉTAPE 5 : Vérification Seuil Activation
**Fichier** : `trader/sltp.py` lignes 2232-2299

```python
# Configuration
act_min_pips = 28.0  # Depuis config
ACTIVATION_PIPS = max(act_min_pips, spread_floor_pips)

# Vérification
if pnl_pips >= ACTIVATION_PIPS:
    # ACTIVATION TRAILING
    apply_dynamic_trailing(...)
```

**Logs attendus** :
```
[TRAILING] Basket abc12345: pnl=+28.0 pips → ACTIVATION trailing
[TRAILING] SL déplacé de 4139.71 → 4139.99 (+28 pips sécurisés)
```

---

## 🐛 Pourquoi le Trailing ne S'Active JAMAIS ?

### Hypothèses (par ordre de probabilité)

#### ❌ Hypothèse #1 : Thread Crash Silencieux
**Symptôme** : Aucun log `[TRAILING_MONITOR]` visible

**Cause possible** :
- Exception non catchée au démarrage
- `trade_executor` ou `mt5_connector` None
- Problème d'import

**Test** : Vérifier logs au démarrage
```bash
grep "TRAILING_MONITOR" logs/*.log
```

**Attendu** :
```
🚀 [TRAILING_MONITOR] Thread démarré ! interval=2.0s
🔄 [TRAILING_MONITOR] Début d'itération...
📊 [TRAILING_MONITOR] Positions récupérées: 5
```

---

#### ❌ Hypothèse #2 : Baskets NON Détectés
**Symptôme** : Log `🎯 [TRAILING_MONITOR] Baskets détectés: set()` (vide)

**Cause possible** :
- Format commentaire incorrect (devrait être `bs_abc12345`)
- Pattern regex ne match pas
- Positions sans commentaire

**Test** : Vérifier commentaires MT5
```bash
grep "BURST_#" logs/*.log | grep "basket_id="
```

**Attendu** :
```
🔍 [SL_TRACE][BURST_#1/5] ... | basket_id=abc12345
```

---

#### ❌ Hypothèse #3 : Fonction `update_basket_sltp_dynamically` Crash
**Symptôme** : Thread tourne mais erreur à chaque appel

**Cause possible** :
- `_resolve_basket_context_for_sltp` retourne None
- Calcul PnL échoue
- Exception non catchée dans sltp.py

**Test** : Chercher erreurs
```bash
grep "update_basket_sltp_dynamically" logs/*.log | grep -i error
```

---

#### ❌ Hypothèse #4 : PnL Mal Calculé (Toujours < 28 pips)
**Symptôme** : Fonction tourne mais PnL jamais >= 28 pips

**Cause possible** :
- Calcul pip_value incorrect pour XAUUSD
- `profit` MT5 en $ mais conversion pips fausse
- Spread_floor_pips élevé retarde activation

**Test** : Vérifier calcul PnL
```bash
grep "BASKET_CTX.*pips" logs/*.log
```

**Attendu** :
```
[BASKET_CTX] MT5_FALLBACK | abc12345 | XAUUSD BUY | 5 pos | 44.5 pips
```

**Si vous voyez** : `| 0.4 pips` au lieu de `| 40.0 pips` → Bug calcul !

---

#### ❌ Hypothèse #5 : `spread_floor_pips` Retarde Activation
**Symptôme** : PnL >= 28 pips mais activation à 50+ pips

**Cause** : Spread élevé + `spread_multiplier > 0`

**Vérification** :
```python
spread_floor_pips = (spread_multiplier × cur_spread_pips) + extra_buffer_pips
ACTIVATION_PIPS = max(28.0, spread_floor_pips)
```

**Solution** : ON A DÉJÀ CORRIGÉ (spread_multiplier=0.0)

---

## 🔧 Plan de Debug

### Test #1 : Vérifier Thread Démarre
**Au démarrage du bot, chercher** :
```bash
grep "🚀 DÉMARRAGE DU THREAD" logs/*.log
grep "🚀 \[TRAILING_MONITOR\] Thread démarré" logs/*.log
```

**Attendu** : 2 lignes avec ces messages

**Si absent** : Thread ne démarre pas → Erreur au lancement

---

### Test #2 : Vérifier Thread Tourne
**Après 10 secondes avec positions ouvertes** :
```bash
grep "🔄 \[TRAILING_MONITOR\] Début d'itération" logs/*.log | wc -l
```

**Attendu** : Au moins 5 lignes (5 itérations en 10s)

**Si 0** : Thread crashé ou bloqué

---

### Test #3 : Vérifier Détection Baskets
```bash
grep "🎯 \[TRAILING_MONITOR\] Baskets détectés" logs/*.log
```

**Attendu** : `Baskets détectés: {'abc12345', ...}`

**Si** : `Baskets détectés: set()` → Commentaires MT5 incorrects

---

### Test #4 : Vérifier Calcul PnL
```bash
grep "BASKET_CTX.*pips" logs/*.log
```

**Attendu** : `| 44.5 pips` (valeur cohérente avec profit réel)

**Si** : `| 0.4 pips` → Bug calcul pip_value

---

### Test #5 : Vérifier Appel Trailing
```bash
grep "🔥 \[DEBUG_TRAILING\] DÉBUT update_basket_sltp_dynamically" logs/*.log
```

**Attendu** : Au moins 1 ligne si baskets détectés

**Si absent** : Fonction jamais appelée

---

## ✅ Prints de Debug Déjà Ajoutés

Nous avons ajouté des prints stratégiques :

1. **trader/sltp.py ligne 273** : `[SL_TRACE][CALC_START]` - Calcul SL/TP initial
2. **trader/sltp.py ligne 781** : `[SL_TRACE][CALC_END]` - Résultat SL/TP
3. **trader/order_builder.py ligne 631** : `[SL_TRACE][ORDER_BUILDER]` - Après calcul
4. **trader/burst.py ligne 69** : `[SL_TRACE][BURST_#X/Y]` - Avant envoi position
5. **trader/trade_executor.py ligne 156** : `[SL_TRACE][MT5_SEND]` - Envoi MT5
6. **trader/trade_executor.py ligne 204** : `[SL_TRACE][POST_FILL]` - Post-fill

**Pour trailing, nous avons** :
- **run_bot.py ligne 2765** : `[TRAILING_MONITOR] Thread démarré`
- **run_bot.py ligne 2774** : `[TRAILING_MONITOR] Début d'itération`
- **run_bot.py ligne 2779** : `[TRAILING_MONITOR] Positions récupérées: X`
- **run_bot.py ligne 2801** : `[TRAILING_MONITOR] Baskets détectés: {...}`
- **trader/sltp.py ligne 2062** : `[DEBUG_TRAILING] DÉBUT update_basket_sltp_dynamically`

---

## 🎯 Prints Supplémentaires à Ajouter

Pour identifier le problème, il faut ajouter des prints dans `update_basket_sltp_dynamically` et `_resolve_basket_context_for_sltp` :

### 1. Vérifier PnL Calculé
**trader/sltp.py après ligne 2100** (calcul PnL basket) :
```python
print(f"🔥 [DEBUG_TRAILING] PnL calculé: {basket_pnl_pips:.2f} pips | ACTIVATION_PIPS={ACTIVATION_PIPS:.2f}", flush=True)
```

### 2. Vérifier Seuil Activation
**trader/sltp.py après ligne 2110** (vérification seuil) :
```python
if basket_pnl_pips >= ACTIVATION_PIPS:
    print(f"✅ [DEBUG_TRAILING] ACTIVATION ! PnL={basket_pnl_pips:.2f} >= {ACTIVATION_PIPS:.2f}", flush=True)
else:
    print(f"⏸️ [DEBUG_TRAILING] Attente activation: PnL={basket_pnl_pips:.2f} < {ACTIVATION_PIPS:.2f}", flush=True)
```

### 3. Vérifier Fallback MT5 Appelé
**trader/sltp.py ligne 979** (début fallback) :
```python
print(f"🔄 [BASKET_CTX][FALLBACK] Tentative récupération basket {basket_id} depuis MT5...", flush=True)
```

### 4. Vérifier Positions Basket Trouvées
**trader/sltp.py après ligne 1000** (filtre positions) :
```python
print(f"🔍 [BASKET_CTX][FALLBACK] {len(basket_positions)} positions trouvées pour basket {basket_id}", flush=True)
```

---

## 📊 Résumé

### Processus Actuel (Théorique)
```
Thread Surveillance (toutes les 2s)
    ↓
Récupère Positions MT5
    ↓
Extrait Basket IDs (bs_abc12345)
    ↓
Pour chaque basket:
    → Appelle update_basket_sltp_dynamically()
    → Calcule PnL du basket
    → Si PnL >= 28 pips:
        → Active trailing stop
        → Déplace SL à prix - 8 pips
```

### Problème Actuel
**Le trailing ne s'active JAMAIS, même à 400 pips.**

### Causes Possibles (à tester)
1. ❌ Thread crash silencieux
2. ❌ Baskets non détectés (format commentaire)
3. ❌ Fonction crash à l'appel
4. ❌ PnL mal calculé (toujours < 28 pips)
5. ❌ Spread_floor_pips retarde activation (CORRIGÉ)

### Prochaine Étape
**Lancer le bot et vérifier logs** :
1. Thread démarre ? `grep "TRAILING_MONITOR.*démarré"`
2. Itérations ? `grep "Début d'itération"`
3. Baskets détectés ? `grep "Baskets détectés"`
4. PnL calculé ? `grep "BASKET_CTX.*pips"`
5. Activation ? `grep "ACTIVATION"`

---

*Analyse créée le 12 Novembre 2025*
