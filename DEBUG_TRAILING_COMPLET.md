# Debug Trailing Stop - Guide Complet

## ✅ Réponse à Votre Question

### Quel est le processus qui active le trailing ?

**OUI !** ✅ Un **thread dédié** surveille le PnL de **TOUS les baskets** en temps réel **toutes les 2 secondes**.

---

## 🔄 Architecture Complète du Système

```
┌─────────────────────────────────────────────────────────────┐
│ THREAD DE SURVEILLANCE (run_bot.py ligne 2738-2838)        │
│ Nom: "TrailingStopMonitor"                                  │
│ Fréquence: 2 secondes                                       │
│ Daemon: true                                                 │
│ État: DÉMARRÉ au lancement du bot (ligne 3040)             │
└──────────────┬──────────────────────────────────────────────┘
               ↓
┌─────────────────────────────────────────────────────────────┐
│ ÉTAPE 1: Récupération Positions MT5                         │
│ - mt5_connector.get_open_positions()                        │
│ - Toutes les 2 secondes                                     │
│                                                              │
│ 📊 LOG: Positions récupérées: 5                             │
└──────────────┬──────────────────────────────────────────────┘
               ↓
┌─────────────────────────────────────────────────────────────┐
│ ÉTAPE 2: Extraction Basket IDs                              │
│ - Pattern regex: bs_([a-f0-9]{8})                           │
│ - Cherche commentaire de chaque position                    │
│                                                              │
│ 🎯 LOG: Baskets détectés: {'abc12345', 'def67890'}          │
└──────────────┬──────────────────────────────────────────────┘
               ↓
┌─────────────────────────────────────────────────────────────┐
│ ÉTAPE 3: Boucle sur Chaque Basket                           │
│ - Pour basket_id in basket_ids:                             │
│   → Appelle trade_executor.update_basket_sltp_dynamically() │
│                                                              │
│ 🔥 LOG: DÉBUT update_basket_sltp_dynamically                │
└──────────────┬──────────────────────────────────────────────┘
               ↓
┌─────────────────────────────────────────────────────────────┐
│ ÉTAPE 4: Résolution Contexte Basket                         │
│ (trader/sltp.py ligne 828: _resolve_basket_context_for_sltp)│
│                                                              │
│ → Essaie burst_manager.get_basket_context() [NON DISPONIBLE]│
│ → FALLBACK MT5 (ligne 987):                                 │
│   1. Récupère toutes positions MT5                          │
│   2. Filtre par basket_id dans commentaire                  │
│   3. Calcule PnL total en pips                              │
│                                                              │
│ 🔄 LOG: Tentative récupération basket abc12345 depuis MT5... │
│ 🔍 LOG: 5 positions trouvées pour basket abc12345           │
│                                                              │
│ CALCUL PNL:                                                  │
│   Pour chaque position:                                     │
│     pip_value = 10.0 × volume    (XAUUSD)                   │
│     pnl_pips = profit / pip_value                           │
│   total_pnl_pips = sum(pnl_pips)                            │
│                                                              │
│ ✅ LOG: Contexte créé | XAUUSD BUY | 5 pos | PnL=44.25 pips │
└──────────────┬──────────────────────────────────────────────┘
               ↓
┌─────────────────────────────────────────────────────────────┐
│ ÉTAPE 5: Vérification Seuils                                │
│ (trader/sltp.py ligne 2110-2127)                            │
│                                                              │
│ pnl_pips = context["basket_pnl_pips"]                       │
│ act_min_pips = 28.0  (depuis config)                        │
│                                                              │
│ 🔥 LOG: PnL=44.25 pips | fill_ratio=1.00                    │
│ 🔥 LOG: act_min_pips=28.00 | loss_min_pips=0.00             │
└──────────────┬──────────────────────────────────────────────┘
               ↓
┌─────────────────────────────────────────────────────────────┐
│ ÉTAPE 6: Décision Activation                                │
│ (trader/sltp.py ligne 2165-2168)                            │
│                                                              │
│ perf_trigger = (pnl_pips >= 28.0) or (pnl_pips <= 0.0)     │
│ should_update = force_refresh OR perf_trigger OR           │
│                 time_ok OR phase_changed OR price_moved     │
│                                                              │
│ 🔍 LOG: [SLTP_DIAGNOSTIC]                                   │
│   pnl_pips=44.25 | act_min_pips=28.00                       │
│   perf_trigger=True | time_ok=True                          │
│   should_update=True                                         │
└──────────────┬──────────────────────────────────────────────┘
               ↓
┌─────────────────────────────────────────────────────────────┐
│ ÉTAPE 7: Application Trailing Stop                          │
│ (trader/sltp.py ligne 2200-2450)                            │
│                                                              │
│ Pour chaque position du basket:                             │
│   → apply_dynamic_trailing()                                │
│   → _calculate_dynamic_trailing()                           │
│   → new_sl = current_price - (8 pips)  [BUY]               │
│   → new_sl = max(new_sl, current_sl)   [Ne jamais empirer]  │
│   → modify_position_sltp(ticket, new_sl, new_tp)           │
│                                                              │
│ 🔥 LOG: [DEBUG_TRAILING] ✅ PAS de trigger                  │
│ ✅ LOG: [TRAILING_MONITOR] Basket abc12345: trailing mis à  │
│         jour (PnL=44.3p)                                     │
└─────────────────────────────────────────────────────────────┘
```

---

## 🔍 Prints de Debug Ajoutés

### 1. Thread de Surveillance (run_bot.py)

| Ligne | Log | Quand |
|-------|-----|-------|
| 2765 | `🚀 [TRAILING_MONITOR] Thread démarré ! interval=2.0s` | Démarrage thread |
| 2774 | `🔄 [TRAILING_MONITOR] Début d'itération...` | Chaque cycle (2s) |
| 2779 | `📊 [TRAILING_MONITOR] Positions récupérées: 5` | Après get_positions |
| 2801 | `🎯 [TRAILING_MONITOR] Baskets détectés: {'abc12345'}` | Extraction baskets |

### 2. Fallback MT5 Contexte (trader/sltp.py)

| Ligne | Log | Quand |
|-------|-----|-------|
| 989 | `🔄 [BASKET_CTX][FALLBACK] Tentative récupération basket abc12345...` | Début fallback |
| 1012 | `🔍 [BASKET_CTX][FALLBACK] 5 positions trouvées pour basket abc12345` | Positions filtrées |
| 1052 | `✅ [BASKET_CTX][FALLBACK] Contexte créé \| XAUUSD BUY \| 5 pos \| PnL=44.25 pips` | Contexte créé |

### 3. Calcul PnL et Décision (trader/sltp.py)

| Ligne | Log | Quand |
|-------|-----|-------|
| 2062 | `🔥 [DEBUG_TRAILING] DÉBUT update_basket_sltp_dynamically` | Entrée fonction |
| 2116 | `🔥 [DEBUG_TRAILING][PNL] basket=abc12345 \| PnL=44.25 pips \| fill_ratio=1.00` | PnL calculé |
| 2127 | `🔥 [DEBUG_TRAILING][SEUILS] act_min_pips=28.00 \| loss_min_pips=0.00` | Seuils config |
| 2171-2174 | `🔍 [SLTP_DIAGNOSTIC] ...` | Diagnostic complet |

### 4. Calcul SL/TP Initial (trader/sltp.py)

| Ligne | Log | Quand |
|-------|-----|-------|
| 273 | `🔍 [SL_TRACE][CALC_START] _calculate_sl_tp_prices() appelée` | Calcul initial |
| 781 | `🔍 [SL_TRACE][CALC_END] SL=4139.71 \| TP=4143.71` | Résultat calcul |

### 5. Envoi Burst (trader/burst.py)

| Ligne | Log | Quand |
|-------|-----|-------|
| 69 | `🔍 [SL_TRACE][BURST_#1/5] Avant envoi \| SL=4139.71 \| basket_id=abc12345` | Avant chaque position |
| 77 | `🔍 [SL_TRACE][BURST_#1/5] Ordre envoyé \| ticket=123456` | Après envoi |

### 6. Envoi MT5 (trader/trade_executor.py)

| Ligne | Log | Quand |
|-------|-----|-------|
| 156 | `🔍 [SL_TRACE][MT5_SEND] Envoi ordre \| symbol=XAUUSD \| SL=4139.71` | Avant order_send |
| 204 | `🔍 [SL_TRACE][POST_FILL] retcode=10009 \| try_attach=False` | Vérif post-fill |

---

## 🐛 Scénarios de Debug

### Scénario 1 : Thread ne Démarre Pas ❌

**Symptôme** : Aucun log `[TRAILING_MONITOR]`

**Commande** :
```bash
grep "🚀 DÉMARRAGE DU THREAD" logs/*.log
grep "🚀 \[TRAILING_MONITOR\] Thread démarré" logs/*.log
```

**Attendu** : 2 lignes

**Si absent** : Erreur au démarrage du thread → Vérifier exception

---

### Scénario 2 : Thread Crash Immédiatement ❌

**Symptôme** : Log démarrage présent mais pas d'itérations

**Commande** :
```bash
grep "🔄 \[TRAILING_MONITOR\] Début d'itération" logs/*.log | wc -l
```

**Attendu** : Au moins 5 en 10 secondes

**Si 0** : Thread crashé → Chercher traceback

---

### Scénario 3 : Baskets NON Détectés ❌

**Symptôme** : Log `Baskets détectés: set()` (vide)

**Commande** :
```bash
grep "🎯 \[TRAILING_MONITOR\] Baskets détectés" logs/*.log
```

**Attendu** : `{'abc12345', ...}`

**Si vide** : Format commentaire incorrect (doit être `bs_<8 hex>`)

---

### Scénario 4 : Fonction Jamais Appelée ❌

**Symptôme** : Baskets détectés mais pas d'appel update_basket_sltp_dynamically

**Commande** :
```bash
grep "🔥 \[DEBUG_TRAILING\] DÉBUT update_basket_sltp_dynamically" logs/*.log
```

**Attendu** : Au moins 1 ligne

**Si absent** : Exception dans l'appel → Chercher erreur

---

### Scénario 5 : Fallback Échoue ❌

**Symptôme** : Fonction appelée mais contexte non créé

**Commande** :
```bash
grep "🔄 \[BASKET_CTX\]\[FALLBACK\] Tentative" logs/*.log
grep "🔍 \[BASKET_CTX\]\[FALLBACK\] .* positions trouvées" logs/*.log
```

**Attendu** :
```
🔄 [BASKET_CTX][FALLBACK] Tentative récupération basket abc12345...
🔍 [BASKET_CTX][FALLBACK] 5 positions trouvées pour basket abc12345
```

**Si 0 positions** : Filtre commentaire ne matche pas

---

### Scénario 6 : PnL Mal Calculé ❌ (PROBABLE)

**Symptôme** : PnL toujours < 28 pips même avec +400 pips réels

**Commande** :
```bash
grep "✅ \[BASKET_CTX\]\[FALLBACK\] Contexte créé" logs/*.log
grep "🔥 \[DEBUG_TRAILING\]\[PNL\]" logs/*.log
```

**Attendu** :
```
✅ [BASKET_CTX][FALLBACK] ... | PnL=44.25 pips
🔥 [DEBUG_TRAILING][PNL] basket=abc12345 | PnL=44.25 pips
```

**Si PnL=0.4 au lieu de 40.0** : Bug calcul pip_value !

**Formule actuelle** :
```python
pip_value = 10.0 * volume  # XAUUSD: 1 pip = 10$ par lot
pnl_pips = profit / pip_value
```

**Exemple XAUUSD** :
- Volume = 0.07 lots
- Profit = +29.40 $
- pip_value = 10.0 × 0.07 = 0.70 $ par pip
- pnl_pips = 29.40 / 0.70 = **42 pips** ✅

**Si vous voyez 0.42 pips** : pip_value calculé à 70 au lieu de 0.70 !

---

### Scénario 7 : Seuil Activation Incorrect ❌

**Symptôme** : PnL >= 28 mais perf_trigger=False

**Commande** :
```bash
grep "🔥 \[DEBUG_TRAILING\]\[SEUILS\]" logs/*.log
grep "🔍 \[SLTP_DIAGNOSTIC\]" logs/*.log -A 3
```

**Attendu** :
```
🔥 [DEBUG_TRAILING][SEUILS] act_min_pips=28.00 | loss_min_pips=0.00
🔍 [SLTP_DIAGNOSTIC] Basket abc12345:
  pnl_pips=44.25 | act_min_pips=28.00 | loss_min_pips=0.00
  perf_trigger=True | ...
  should_update=True
```

**Si act_min_pips != 28.00** : Config mal lue !

---

## ✅ Tests à Effectuer Maintenant

### Test 1 : Thread Tourne
```bash
# Lancer le bot et attendre 10 secondes
grep "🔄 \[TRAILING_MONITOR\] Début d'itération" logs/*.log | wc -l
# Attendu: au moins 5
```

### Test 2 : Baskets Détectés
```bash
# Avec 1 panier ouvert
grep "🎯 \[TRAILING_MONITOR\] Baskets détectés" logs/*.log | tail -1
# Attendu: Baskets détectés: {'abc12345'}
```

### Test 3 : PnL Calculé
```bash
# Avec trade en profit +30 pips
grep "✅ \[BASKET_CTX\]\[FALLBACK\].*PnL=" logs/*.log | tail -1
# Attendu: PnL=30.XX pips (cohérent avec MT5)
```

### Test 4 : Activation Trailing
```bash
# Avec trade >= +28 pips
grep "perf_trigger=True" logs/*.log
# Attendu: au moins 1 ligne si PnL >= 28
```

---

## 🎯 Diagnostic Final

### Si le Trailing ne S'Active JAMAIS

**Causes par ordre de probabilité** :

1. ✅ **Thread tourne** (on va vérifier)
2. ✅ **Baskets détectés** (format commentaire OK)
3. ❓ **PnL mal calculé** → **TRÈS PROBABLE** si vous voyez 0.4 au lieu de 40 pips
4. ❓ **Seuil activation incorrect** → act_min_pips != 28.0
5. ❓ **Fonction crash silencieusement** → Exception non catchée

---

## 📋 Prochaines Étapes

1. **Lancer le bot** avec les prints ajoutés
2. **Ouvrir un trade** et le laisser courir jusqu'à +30-40 pips
3. **Récupérer logs** et chercher :
   - `🔄 [TRAILING_MONITOR]` → Thread actif ?
   - `✅ [BASKET_CTX][FALLBACK].*PnL=` → PnL correct ?
   - `🔥 [DEBUG_TRAILING][PNL]` → PnL passé à la fonction ?
   - `perf_trigger=True` → Activation détectée ?

4. **M'envoyer les logs** pour analyse si problème persiste

---

*Document créé le 12 Novembre 2025*
