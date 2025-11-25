# ⚡ Déploiement Optimisations Latence - Phase 1

**Date:** 2025-11-25
**Objectif:** Réduire latence console → MT5 de ~150ms à ~80-100ms

---

## ✅ Modifications Effectuées (3 fichiers)

### 1. trader/burst.py

**Ligne 89-90** - Réduction du sleep entre positions:
```python
# AVANT:
_t.sleep(float(self.config_manager.get("burst_send_sleep_s", 0.01) or 0.01))

# APRÈS:
# ⚡ OPTIMISATION LATENCE: Réduit à 0.005s (5ms) pour gain de ~40ms sur 8 positions
_t.sleep(float(self.config_manager.get("burst_send_sleep_s", 0.005) or 0.005))
```

**Gain:** 8 positions × 5ms économisés = **40ms**

---

### 2. run_bot.py

**Ligne 2213-2244** - Cache config SLTP:
```python
# ⚡ OPTIMISATION LATENCE: Cache config SLTP (gain ~5-10ms)
# Évite de re-parser la config à chaque trade
cache_key = f"_sltp_cfg_{sym}"
sltp_cfg = global_context.get(cache_key)

if sltp_cfg is None:
    # Premier calcul: parser la config (coûteux)
    sltp_cfg = (
        (
            (global_context.get("asset_configs", {}) or {}).get(
                sym, {}
            )
            or {}
        ).get("entry_rules", {})
        or {}
    ).get("scalping", {}) or {}
    sltp_cfg = (sltp_cfg.get("burst_scalping", {}) or {}).get(
        "sltp", {}
    ) or (
        (
            base_config.get("entry_rules", {})
            .get("scalping", {})
            .get("burst_scalping", {})
            .get("sltp", {})
        )
        or {}
    )
    # Stocker dans le cache
    global_context[cache_key] = sltp_cfg

if sltp_cfg:
    td["sltp"] = sltp_cfg
```

**Gain:** ~5-10ms (config parsée 1 fois au lieu de chaque trade)

---

### 3. trader/burst.py (déjà optimal)

**Lignes 59-66** - SL/TP pré-calculés:
```python
def _base_req_copy():
    r = dict(base_request)
    r["comment"] = comment
    # SL/TP déjà calculés AVANT la boucle dans run_bot.py
    r["sl"] = float(base_request.get("sl", 0.0) or 0.0)
    if base_request.get("tp") is not None:
        r["tp"] = float(base_request.get("tp", 0.0) or 0.0)
    return r
```

**Gain:** Déjà optimisé (SL/TP calculés 1 fois dans run_bot.py avant l'appel à burst)

---

## 📊 Résultats Attendus

### Avant (latence totale: ~150-200ms)
- Construction requête: 10-20ms
- Calcul SL/TP: 20-40ms (déjà optimisé)
- Envoi 8 positions: 80-120ms (8 × 10ms send + 8 × 10ms sleep)
- Lecture config: 5-10ms
- Réseau MT5: 10-50ms

### Après Phase 1 (latence totale: ~80-120ms)
- Construction requête: 10-20ms
- Calcul SL/TP: 20-40ms (déjà optimisé)
- Envoi 8 positions: **40-80ms** (8 × 10ms send + 8 × 5ms sleep) ✅ **-40ms**
- Lecture config: **0ms** (cache) ✅ **-10ms**
- Réseau MT5: 10-50ms

**Gain total Phase 1: ~50ms (-30% à -40%)**

---

## 🔄 Procédure de Déploiement

### 1. Copier les fichiers modifiés sur le VPS:
- `trader/burst.py`
- `run_bot.py`

### 2. Redémarrer le bot

### 3. Vérifier dans les logs:
- Aucun message "Trade context busy" (si présent → sleep trop bas)
- Temps entre "[FUSION][FAST-LANE]" et les tickets MT5 doit être réduit

### 4. Mesurer la latence:
Comparer le timestamp du log `[FUSION][FAST-LANE]` avec le timestamp d'ouverture MT5 du premier ticket.

**Exemple:**
```
2025-11-25 14:32:18.123 [FUSION][FAST-LANE] BUY XAUUSD burst=8
2025-11-25 14:32:18.203 [BURST] ✅ 8 positions ouvertes
```
Latence = 203 - 123 = **80ms** (au lieu de 150ms avant)

---

## ⚠️ Points d'Attention

### Si "Trade context busy" apparaît:

Le sleep de 5ms est trop court. Remonter progressivement:

**trader/burst.py ligne 90:**
```python
# Remonter à 7ms si 5ms cause des rejets
_t.sleep(float(self.config_manager.get("burst_send_sleep_s", 0.007) or 0.007))
```

### Si changement de config en cours de session:

Le cache SLTP est permanent jusqu'au redémarrage du bot. Si vous modifiez la config SL/TP:
1. Soit: redémarrer le bot
2. Soit: utiliser le hot-reload (kill -SIGUSR1 sur Linux, ou redémarrer)

---

## 🚀 Phase 2 (Optionnelle)

Si latence encore > 50ms après Phase 1, implémenter:

### Solution 4: Envoi parallèle des 8 positions
**Gain:** -80ms supplémentaires (latence totale < 20ms)
**Risque:** Plus complexe, peut causer rejets MT5

Voir fichier `REDUCTION_LATENCE.md` pour l'implémentation.

---

## 📝 Fichiers Modifiés

1. `trader/burst.py` (ligne 90)
2. `run_bot.py` (lignes 2213-2244)

---

## ✅ Tests à Effectuer

1. **Aucun rejet MT5:**
   - Vérifier que toutes les 8 positions s'ouvrent
   - Aucun log "Trade context busy"

2. **Latence réduite:**
   - Mesurer temps entre log FUSION et confirmation MT5
   - Objectif: < 100ms (au lieu de 150-200ms)

3. **SL/TP corrects:**
   - Vérifier que SL = 400 pips et TP = 400 pips sur toutes les positions

4. **Pas de régression:**
   - Scores toujours corrects dans trades_history.jsonl
   - TradeLogger fonctionne normalement

---

## 💡 Prochaine Étape

Si vous voulez passer à la Phase 2 (envoi parallèle, gain ~80ms supplémentaires pour atteindre < 20ms de latence), dites-le moi et je vous fournirai le code complet.

Sinon, testez d'abord Phase 1 et mesurez la latence réelle sur le VPS.
