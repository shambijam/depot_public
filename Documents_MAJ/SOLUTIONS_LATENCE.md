# 🚀 Solutions pour Réduire la Latence et Éviter les Doubles Trades

**Date:** 2025-11-25
**Problème:** Bot prend 2 trades consécutifs sur le même signal, latence entre console et MT5

---

## 📊 Analyse de la Latence Actuelle

### Sources de latence identifiées:

1. **Console → MT5: 100-200ms**
   - Construction de la requête burst (td dict, fusion_data)
   - Calcul SL/TP (400 pips, trailing params)
   - Envoi séquentiel de 8 positions avec sleep(0.01s) entre chaque
   - Total: 8 × (send + 0.01s) ≈ 100-200ms

2. **Analyse → Décision: variable**
   - Bougie M1 déjà en formation au moment de l'analyse
   - Signal peut changer pendant l'analyse multi-composants (OrderFlow + Footprint + Triggers)

3. **Réseau MT5: 10-50ms (normal) jusqu'à 500ms (haute volatilité)**
   - Latence serveur MT5
   - Slippage possible sur MARKET orders

4. **Double analyse du même signal:**
   - Cooldown actuel = 30s APRÈS sortie du trade
   - Pas de cooldown AVANT l'entrée
   - Même bougie M1 analysée 2 fois avant que le cooldown ne s'active
   - Résultat: 2 trades consécutifs sur le même setup

---

## ✅ Solution 1: Verrou d'Exécution (Empêcher Double Analyse)

### Objectif
Bloquer toute nouvelle analyse pendant qu'un trade est en cours d'exécution.

### Implémentation dans `run_bot.py`

**Ligne 57** - Ajouter les variables de verrou:
```python
# === ÉTAT GLOBAL ===
last_cycle_ts = None
bursts = {}
_asset_analysis_states = {}  # Timestamp de la dernière analyse par actif

# ✅ NOUVEAU: Verrous d'exécution pour éviter doubles trades
_execution_locks = {}  # {asset: timestamp_execution}
_last_candle_traded = {}  # {asset: candle_timestamp}
```

**Ligne 1420** (avant l'appel à FusionManager) - Vérifier le verrou:
```python
# === FUSION MANAGER (XAUUSD uniquement) ===
if asset.upper() == "XAUUSD" and _fusion_mgr and hasattr(_fusion_mgr, "fuse"):
    try:
        # ✅ VERROU 1: Bloquer si trade en cours d'exécution
        execution_lock_time = _execution_locks.get(asset, 0)
        now_ts = time.time()

        if now_ts - execution_lock_time < 5.0:  # 5s de blocage pendant exécution
            logger.info(f"[FUSION][{asset}] ⏳ Trade en cours d'exécution, skip analyse (lock actif)")
            continue

        # ✅ VERROU 2: Bloquer si même bougie M1 déjà tradée
        candle_ts = latest.get("timestamp")  # Timestamp de la bougie M1 actuelle
        if candle_ts and _last_candle_traded.get(asset) == candle_ts:
            logger.info(f"[FUSION][{asset}] ⏸️ Bougie M1 déjà tradée (ts={candle_ts}), skip")
            continue

        # ✅ VERROU 3: Cooldown avant trade (pas seulement après exit)
        last_burst_time = getattr(_fusion_mgr, "_last_burst_time", 0) or 0
        cooldown_s = 60  # 60s entre chaque trade (au lieu de 30s après exit)

        if now_ts - last_burst_time < cooldown_s:
            remaining = int(cooldown_s - (now_ts - last_burst_time))
            logger.info(f"[FUSION][{asset}] ⏰ Cooldown actif, {remaining}s restantes")
            continue

        # Si tous les verrous sont OK, continuer avec FusionManager
        out = _fusion_mgr.fuse(
            orderflow=raw_orderflow,
            footprint=raw_footprint,
            triggers=trig,
            signals=signals,
        )
        # ... (reste du code existant)
```

**Ligne 2185** (juste avant la création du td pour burst) - Activer les verrous:
```python
resolved_burst = max(int(_resolve(sym)), 1)

# ✅ ACTIVER LES VERROUS: Trade en cours d'exécution
_execution_locks[sym] = time.time()
_last_candle_traded[sym] = latest.get("timestamp")

logger.info(f"🔒 [FUSION][{sym}] Verrous activés: execution_lock + candle_lock")

td = {
    "rule_name": "burst_scalping",
    # ... (reste du code existant)
```

**Ligne 2280** (après l'exécution du burst) - Libérer le verrou:
```python
# Exécuter le burst
result = open_burst_basket(
    self=trade_executor,
    base_request=mt5_request,
    decision_package=td,
    mt5_connector=mt5_connector,
)

# ✅ LIBÉRER LE VERROU: Trade terminé (succès ou échec)
_execution_locks.pop(sym, None)
logger.info(f"🔓 [FUSION][{sym}] Verrou d'exécution libéré")

# Résultat
if isinstance(result, dict) and result.get("tickets"):
    logger.info(
        f"[BURST] ✅ {len(result['tickets'])} positions ouvertes basket={result.get('basket_id')}"
    )
```

---

## ✅ Solution 2: Cooldown AVANT Trade (Pas Seulement Après Exit)

### Problème Actuel
Le cooldown de 30s ne s'active qu'APRÈS la sortie du trade, pas entre les entrées.

### Solution
Déplacer le cooldown AVANT l'analyse, en utilisant `_last_burst_time` comme référence.

**C'est déjà implémenté dans Solution 1 (Verrou 3)**, ligne:
```python
last_burst_time = getattr(_fusion_mgr, "_last_burst_time", 0) or 0
cooldown_s = 60  # 60s entre chaque trade

if now_ts - last_burst_time < cooldown_s:
    remaining = int(cooldown_s - (now_ts - last_burst_time))
    logger.info(f"[FUSION][{asset}] ⏰ Cooldown actif, {remaining}s restantes")
    continue
```

---

## ✅ Solution 3: Analyser la Bougie M1 Précédente (Pas la Courante)

### Problème
La bougie M1 actuelle est en formation → le signal peut changer pendant l'analyse.

### Solution
Analyser la bougie M1 **fermée** (précédente) au lieu de la bougie en cours.

**Avantages:**
- Données stables et complètes
- Pas de changement de signal pendant l'analyse
- Latence réduite (pas d'attente de clôture)

**Inconvénient:**
- Exécution avec 1 minute de retard (acceptable pour scalping si le signal reste valide)

### Implémentation dans `run_bot.py`

**Ligne 1350** (récupération de latest) - Utiliser la bougie précédente:
```python
# ✅ MODIFIER: Utiliser la bougie M1 fermée (n-1) au lieu de la courante (n)
# Raison: Éviter que le signal change pendant l'analyse
latest_m1 = signals.get("__latest__", {})

# Vérifier qu'on a bien l'historique M1
m1_history = signals.get("M1", {}).get("history", [])

if len(m1_history) >= 2:
    # Prendre l'avant-dernière bougie (fermée, stable)
    latest = m1_history[-2]
    logger.info(f"[FUSION][{asset}] ⏮️ Analyse bougie M1 fermée (n-1)")
else:
    # Fallback: utiliser la bougie courante si historique insuffisant
    latest = latest_m1
    logger.warning(f"[FUSION][{asset}] ⚠️ Historique M1 insuffisant, analyse bougie courante")
```

**Note:** Cette modification nécessite de vérifier que l'OrderFlow, Footprint et Triggers utilisent bien le même `latest` (la bougie n-1).

---

## ✅ Solution 4: Optimiser l'Envoi Burst (Réduire la Latence MT5)

### Problème
8 positions envoyées séquentiellement avec `sleep(0.01s)` entre chaque = ~80-160ms total.

### Solution A: Réduire le sleep à 0.005s (5ms)
**Gain:** ~40ms sur 8 positions

**Implémentation dans `trader/burst.py` ligne 90:**
```python
# AVANT:
_t.sleep(float(self.config_manager.get("burst_send_sleep_s", 0.01) or 0.01))

# APRÈS:
_t.sleep(float(self.config_manager.get("burst_send_sleep_s", 0.005) or 0.005))
```

**Risque:** "Trade context busy" si MT5 ne suit pas. Tester d'abord.

### Solution B: Envoi parallèle (avancé)
Envoyer les 8 positions en parallèle avec threading.

**Gain:** ~100-120ms (envoi quasi-instantané)

**Risque:** Plus complexe, peut causer des rejets MT5 ("trade context busy").

**Implémentation (optionnelle):**
```python
import threading

def _send_burst_position(self, mt5_connector, req_copy):
    """Envoie une position burst en thread séparé."""
    try:
        result = mt5_connector.send_order(req_copy)
        return result
    except Exception as e:
        self.logger.error(f"Erreur envoi burst: {e}")
        return None

# Dans open_burst_basket(), remplacer la boucle:
threads = []
results = []

for i in range(burst_size):
    req_copy = {**base_request, "comment": f"{basket_id}"}

    thread = threading.Thread(
        target=lambda r=req_copy: results.append(self._send_burst_position(mt5_connector, r))
    )
    threads.append(thread)
    thread.start()

    # Petit délai pour éviter "trade context busy"
    _t.sleep(0.002)  # 2ms au lieu de 10ms

# Attendre la fin de tous les threads
for thread in threads:
    thread.join(timeout=2.0)
```

**Recommandation:** Commencer par Solution A (réduire sleep à 5ms), puis tester Solution B si nécessaire.

---

## 📦 Résumé des Modifications à Déployer

### Fichier 1: `run_bot.py`

**Ligne 57** - Ajouter les verrous:
```python
_execution_locks = {}
_last_candle_traded = {}
```

**Ligne 1420** - Ajouter les 3 vérifications:
1. Verrou exécution (5s)
2. Verrou bougie M1 déjà tradée
3. Cooldown avant trade (60s)

**Ligne 2185** - Activer les verrous:
```python
_execution_locks[sym] = time.time()
_last_candle_traded[sym] = latest.get("timestamp")
```

**Ligne 2280** - Libérer le verrou:
```python
_execution_locks.pop(sym, None)
```

### Fichier 2: `trader/burst.py`

**Ligne 90** - Réduire le sleep:
```python
_t.sleep(float(self.config_manager.get("burst_send_sleep_s", 0.005) or 0.005))
```

### Optionnel: Analyser bougie M1 précédente

**Ligne 1350 dans `run_bot.py`** - Utiliser `m1_history[-2]` au lieu de `latest`

---

## 🎯 Résultats Attendus

### Avant
- Latence console → MT5: **150-200ms**
- 2 trades consécutifs sur le même signal
- Cooldown inefficace (30s après exit seulement)

### Après
- Latence console → MT5: **80-120ms** (-40% à -60%)
- 1 seul trade par signal (double-trade impossible)
- Cooldown avant trade: **60s** entre chaque entrée
- Même bougie M1 ne peut être tradée 2 fois

---

## ⚠️ Points d'Attention

1. **Tester le sleep à 5ms**
   Si MT5 rejette avec "trade context busy", revenir à 10ms.

2. **Vérifier que latest.get("timestamp") existe**
   Sinon le verrou bougie ne fonctionnera pas.

3. **Cooldown à 60s peut réduire le nombre de trades**
   C'est voulu pour éviter le over-trading (163 trades en 3h).

4. **Ne pas oublier de libérer le verrou en cas d'erreur**
   Ajouter un `try/finally` si nécessaire.

---

## 🔄 Procédure de Test

1. **Déployer les modifications sur VPS**
2. **Redémarrer le bot**
3. **Observer les logs:**
   - `🔒 Verrous activés: execution_lock + candle_lock`
   - `⏰ Cooldown actif, Xs restantes`
   - `⏸️ Bougie M1 déjà tradée`
   - `🔓 Verrou d'exécution libéré`
4. **Vérifier qu'il n'y a plus de doubles trades consécutifs**
5. **Mesurer la latence:** comparer timestamp console vs timestamp MT5

---

## 📞 Questions?

Si vous voulez implémenter la Solution 3 (analyser bougie M1 précédente) ou la Solution 4B (envoi parallèle), dites-le moi et je vous fournirai le code complet.
