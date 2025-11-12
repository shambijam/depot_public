# 🔍 Analyse Complète - Bug SL=0.0 sur Premier Burst (Comportement Intermittent)

**Date** : 12 Novembre 2025
**Symptôme** : Première position d'un burst créée **SANS SL** (SL=0.0), positions suivantes OK (SL=4139.71)
**Impact** : Position 1 exposée à risque illimité, trailing stop ne peut pas s'activer

---

## 📊 I. Observation - Logs Réels

### Basket `889d4958` (5 positions XAUUSD BUY)

**Position #1 (Ticket 199806121)** :
```
entry: 4143.76
sl: 0.0      ← AUCUN SL !
tp: 7375.89  ← TP ABERRANT !
volume: 0.07
```

**Positions #2-5 (Tickets 199806125, 128, 133, 137)** :
```
entry: ~4143.76
sl: 4139.71     ← SL CORRECT (400 pips)
tp: ~4156.xx    ← TP CORRECT (1215 pips avec RR=3.0)
volume: 0.07
```

### ✅ Ce Qui Est Correct

- **Positions 2-5** : SL et TP corrects (distance ~400 pips)
- **Configuration** : `config_trade_scalping.json` spécifie bien SL/TP 400 pips
- **Calcul** : `_calculate_sl_tp_prices` retourne les bonnes valeurs

### ❌ Ce Qui Est Incorrect

- **Position 1** : SL=0.0 au lieu de 4139.71
- **Position 1** : TP=7375.89 aberrant (devrait être 4147.76)
- **Comportement** : **INTERMITTENT** (pas toujours, seulement parfois)

---

## 🔎 II. Investigation - Chaîne de Code

### Étape 1 : Calcul SL/TP (`trader/sltp.py` ligne 253-776)

**Fonction** : `_calculate_sl_tp_prices`

**Code Critique (ligne 772-776)** :
```python
# ---------- 9) Sortie ----------
stop_loss_price = round(float(stop_loss_price), digits)
take_profit_price = (
    None if take_profit_price is None else round(float(take_profit_price), digits)
)
return float(stop_loss_price), take_profit_price
```

**✅ RÉSULTAT** : Retourne TOUJOURS un float pour `stop_loss_price` (jamais None).

---

### Étape 2 : Validation SL (`trader/order_builder.py` ligne 620-635)

**Code** :
```python
try:
    sl_price, tp_price = _calculate_sl_tp_prices(
        self,
        trade_decision=trade_decision,
        config=active_config,
        symbol_info=symbol_info,
        entry_price=entry_price_market,
        market_context=market_context,
        basket_context=basket_ctx,
    )
except Exception as e:
    self.logger.error(f"[ORDER_BUILDER] _calculate_sl_tp_prices error: {e}")
    raise TradeExecutionError(f"Échec calcul SL/TP: {e}")

# Validation stricte du SL (obligatoire)
if not (isinstance(sl_price, (int, float)) and sl_price > 0):
    raise TradeExecutionError("SL requis mais introuvable (calcul SL/TP).")
```

**✅ RÉSULTAT** : Exception levée si `sl_price` invalide → Impossible que `sl_price` soit None ici.

---

### Étape 3 : Construction Requête (`trader/order_builder.py` ligne 1216-1244)

**Code** :
```python
request = {
    "symbol": symbol_info.name,
    "volume": float(vol),
    "magic": ...,
    "sl": float(sl_price),  # ← LIGNE 1228
    "type_time": ORDER_TIME_GTC,
    "deviation": int(deviation_points),
    "comment": comment,
    ...
}
if has_tp:
    request["tp"] = float(tp_price)  # ← LIGNE 1244
```

**✅ RÉSULTAT** : `request["sl"]` = float valide (ex: 4139.71).

---

### Étape 4 : Copie Burst (`trader/burst.py` ligne 54-61)

**Code** :
```python
def _base_req_copy():
    r = dict(base_request)
    r["comment"] = comment
    # On s'assure que SL/TP existent tels que préparés par order_builder
    r["sl"] = float(base_request.get("sl", 0.0) or 0.0)  # ← LIGNE 58
    if base_request.get("tp") is not None:
        r["tp"] = float(base_request.get("tp", 0.0) or 0.0)
    return r
```

**⚠️ PROBLÈME POTENTIEL** :
- Si `base_request["sl"]` est `None` → `r["sl"] = 0.0`
- **MAIS** on a vu que `sl_price` ne peut pas être None (validation ligne 634)
- **DONC** `base_request["sl"]` ne peut pas être None

**✅ RÉSULTAT** : Toutes les 5 requêtes ont `sl=4139.71`.

---

### Étape 5 : Envoi MT5 (`trader/trade_executor.py` ligne 120-243)

**Fonction** : `execute_order`

**Code Critique (ligne 132-150)** :
```python
# Nettoyage/robustesse SL/TP
sl = request.get("sl")
tp = request.get("tp")
if sl is not None or tp is not None:
    try:
        si = self.mt5_connector.symbol_info(symbol)
        digits = int(getattr(si, "digits", 5) or 5) if si else 5
    except Exception:
        digits = 5
    request["sl"] = (
        round(float(sl), digits)
        if isinstance(sl, (int, float))
        else 0.0 if sl else 0.0
    )
    request["tp"] = (
        round(float(tp), digits)
        if isinstance(tp, (int, float))
        else 0.0 if tp else 0.0
    )

# Envoi
result = self.mt5_connector.order_send(request)
```

**✅ RÉSULTAT** : MT5 reçoit une requête avec `sl=4139.71`.

---

### Étape 6 : Traitement Retcode MT5 (`trader/trade_executor.py` ligne 164-217)

**Code** :
```python
retcode = getattr(result, "retcode", None)

# Codes MT5
RET_DONE = 10009           # Trade executed successfully
RET_PLACED = 10008         # Order placed
RET_DONE_PARTIAL = 10010   # Partial execution
RET_INVALID_STOPS = 10016  # Invalid SL/TP levels

summary = {
    "status": (
        "sent"
        if retcode in (RET_DONE, RET_PLACED, RET_DONE_PARTIAL)
        else "failed"
    ),
    ...
}

# Si SL/TP invalides à l'envoi mais ordre rempli, tenter l'attache post-fill
try_attach = (
    retcode in (RET_DONE, RET_DONE_PARTIAL)      # ← retcode = 10009 ou 10010
    and (request.get("sl") or request.get("tp"))
    and retcode == RET_INVALID_STOPS              # ← retcode = 10016
)
if try_attach:
    # ... code post-fill ...
```

## 🚨 **BUG CRITIQUE IDENTIFIÉ !**

### Ligne 197-202 : Logique Incorrecte

```python
try_attach = (
    retcode in (RET_DONE, RET_DONE_PARTIAL)      # retcode ∈ {10009, 10010}
    and (request.get("sl") or request.get("tp"))
    and retcode == RET_INVALID_STOPS              # retcode = 10016
)
```

**PROBLÈME** :
- Condition 1 : `retcode in (10009, 10010)` → True si retcode = 10009
- Condition 3 : `retcode == 10016` → False si retcode = 10009

**RÉSULTAT** : `try_attach` est **TOUJOURS False** !

**La condition devrait être** :
```python
try_attach = (
    retcode == RET_INVALID_STOPS                  # retcode = 10016
    and (request.get("sl") or request.get("tp"))
)
```

**OU** :
```python
try_attach = (
    retcode in (RET_DONE, RET_DONE_PARTIAL, RET_INVALID_STOPS)
    and (request.get("sl") or request.get("tp"))
)
```

---

## 💡 III. Scénario du Bug (Explication Complète)

### Cas Normal (Spread Bas : 2-5 pips)

**Séquence** :

1. **Préparation** :
   - `order_builder` crée `base_request` avec `sl=4139.71, tp=4147.76`

2. **Position 1** :
   - `burst.py` envoie requête avec `sl=4139.71`
   - MT5 **ACCEPTE** le SL (spread OK)
   - `retcode = 10009` (TRADE_RETCODE_DONE)
   - Position créée : **SL=4139.71** ✅

3. **Positions 2-5** :
   - Même processus
   - Positions créées : **SL=4139.71** ✅

**RÉSULTAT** : Toutes les 5 positions ont SL correct.

---

### Cas Bug (Spread Élevé : 8-15 pips)

**Séquence** :

1. **Préparation** :
   - `order_builder` crée `base_request` avec `sl=4139.71, tp=4147.76`

2. **Position 1** (au moment où spread est élevé) :
   - `burst.py` envoie requête avec `sl=4139.71`
   - MT5 **REJETTE** le SL (distance trop proche à cause spread)
   - `retcode = 10016` (TRADE_RETCODE_INVALID_STOPS)
   - MT5 crée position **SANS SL** → **SL=0.0** ❌
   - `try_attach` = False (à cause du bug ligne 201-202)
   - **Aucune tentative de post-fill** ❌

3. **Positions 2-5** (spread redevenu normal) :
   - MT5 **ACCEPTE** le SL
   - `retcode = 10009` (TRADE_RETCODE_DONE)
   - Positions créées : **SL=4139.71** ✅

**RÉSULTAT** : Position 1 avec SL=0.0, positions 2-5 avec SL correct.

---

### Pourquoi le TP est aussi aberrant (7375.89) ?

**Hypothèse** :
- Même problème : MT5 rejette TP avec `INVALID_STOPS`
- MT5 met une valeur par défaut incorrecte
- **OU** : Bug différent dans calcul TP (division manquante ?)

**Besoin** : Vérifier logs détaillés pour position #199806121.

---

## 🎯 IV. Solution - Corriger le Bug Ligne 201-202

### Changement Requis

**AVANT (trader/trade_executor.py ligne 197-217)** :
```python
try_attach = (
    retcode in (RET_DONE, RET_DONE_PARTIAL)      # ← INCORRECT
    and (request.get("sl") or request.get("tp"))
    and retcode == RET_INVALID_STOPS
)
```

**APRÈS (CORRIGÉ)** :
```python
# Si SL/TP invalides à l'envoi, tenter l'attache post-fill
try_attach = (
    retcode == RET_INVALID_STOPS                  # ✅ CORRECT
    and (request.get("sl") or request.get("tp"))
)
```

### Comportement Après Correction

**Spread Élevé** :
1. Position 1 : MT5 renvoie `retcode=10016` (INVALID_STOPS)
2. `try_attach` = True
3. Code attache SL/TP en **post-fill** via `modify_position_stops`
4. Position créée : **SL=4139.71** ✅

**RÉSULTAT** : Toutes les 5 positions ont SL correct, même si spread élevé.

---

## 📋 V. Vérification Supplémentaire Nécessaire

### 1. Vérifier `modify_position_stops` Fonctionne

**Code** (ligne 209-213) :
```python
self.mt5_connector.modify_position_stops(
    position=pos_id,
    sl=request.get("sl") or 0.0,
    tp=request.get("tp") or 0.0,
)
```

**Question** : La fonction `modify_position_stops` existe-t-elle dans `mt5_connector.py` ?

**Recherche nécessaire** : Vérifier que cette fonction est bien implémentée.

---

### 2. Vérifier Logs Broker pour Position #199806121

**Actions** :
1. Lire logs complets MT5 au moment de création position 1
2. Identifier le `retcode` exact
3. Confirmer `retcode=10016` (INVALID_STOPS)

---

## 🧪 VI. Tests de Validation

### Test 1 : Spread Normal

**Conditions** :
- Spread XAUUSD : 2-5 pips
- Ouvrir burst de 5 positions

**Résultat Attendu** :
- Toutes positions : SL correct (aucun post-fill nécessaire)

---

### Test 2 : Spread Élevé (Simulation)

**Conditions** :
- Forcer spread élevé temporairement
- Ouvrir burst de 5 positions

**Résultat Attendu** :
```
[POST-FILL] tentative attache SL/TP.
[POST-FILL] Position 199806121: SL attached 4139.71, TP attached 4147.76
```

**Validation** :
- Toutes positions : SL=4139.71 ✅

---

### Test 3 : Vérification MT5 Real

**Actions** :
1. Appliquer le fix
2. Déployer sur Windows VPS
3. Attendre burst suivant
4. Vérifier SL de **toutes les positions** via MT5

**Validation** :
- Position 1 : SL correct
- Positions 2-5 : SL correct

---

## 📊 VII. Impact du Bug

### Impact Actuel

**Risque** :
- Position 1 exposée à **risque illimité** (aucun SL)
- Si prix va contre trade → **perte massive possible**
- Trailing stop **ne peut pas s'activer** (SL=0.0)

**Fréquence** :
- Dépend du spread XAUUSD au moment exact de la première position
- Estimé : **10-30% des bursts** affectés (spread élevé assez fréquent sur XAUUSD)

**Durée** :
- Position reste sans SL **jusqu'à fermeture manuelle ou trailing**
- Mais trailing ne peut pas s'activer si SL=0.0 → **Position bloquée**

---

### Impact Après Correction

**Risque** :
- ✅ Toutes positions avec SL correct
- ✅ Trailing peut s'activer normalement
- ✅ Risque maîtrisé

**Fiabilité** :
- ✅ Burst fonctionne même avec spread élevé
- ✅ Pas de différence entre position 1 et positions 2-5

---

## ⏱️ VIII. Roadmap de Correction

### Phase 1 : Fix Immédiat (10 min)

**Actions** :
1. ✅ Modifier `trader/trade_executor.py` ligne 197-202
2. ✅ Remplacer condition `try_attach`
3. ✅ Tester localement (import sans erreur)

---

### Phase 2 : Vérification `modify_position_stops` (10 min)

**Actions** :
1. ✅ Chercher fonction dans `mt5_connector.py`
2. ✅ Vérifier signature et comportement
3. ✅ Si manquante : utiliser `modify_position_sltp` à la place

---

### Phase 3 : Tests Locaux (20 min)

**Actions** :
1. ✅ Créer script de test simulant spread élevé
2. ✅ Vérifier `try_attach` = True
3. ✅ Vérifier post-fill appelé

---

### Phase 4 : Déploiement VPS (5 min)

**Actions** :
1. ✅ Sync fichiers vers Windows VPS
2. ✅ Redémarrer bot
3. ✅ Attendre prochain burst

---

### Phase 5 : Validation Production (surveillance)

**Actions** :
1. ✅ Vérifier logs `[POST-FILL]`
2. ✅ Vérifier SL de toutes positions via MT5
3. ✅ Confirmer bug résolu

---

## 🎯 IX. Résumé Exécutif

### Problème

**Symptôme** : Première position d'un burst créée sans SL (SL=0.0) de façon intermittente.

**Cause** : Bug ligne 201-202 de `trader/trade_executor.py` - Condition `try_attach` toujours False.

**Impact** : Position 1 exposée à risque illimité, trailing ne peut pas s'activer.

---

### Solution

**Fix** : Corriger condition ligne 197-202 :
```python
# AVANT
try_attach = (retcode in (RET_DONE, RET_DONE_PARTIAL) and ... and retcode == RET_INVALID_STOPS)

# APRÈS
try_attach = (retcode == RET_INVALID_STOPS and ...)
```

**Résultat** : Post-fill SL/TP activé quand MT5 rejette SL → Toutes positions avec SL correct.

---

### Temps Estimé

- **Fix** : 10 minutes
- **Vérification** : 10 minutes
- **Tests** : 20 minutes
- **Déploiement** : 5 minutes
- **TOTAL** : **45 minutes** ⏱️

---

**Prochaine étape** : Appliquer le fix et tester.

---

*Document créé le : 12 Novembre 2025*
*Dernière mise à jour : Maintenant*
