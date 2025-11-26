# 🔧 Correction Scores à 0% - Déploiement Final

**Date:** 2025-11-25
**Problème:** fusion_data vide ou incomplet (manque components, trigger_boost, etc.)

---

## ✅ Correction Effectuée

### Fichier: `run_bot.py`

**Lignes 1489-1507** - Garder `out` complet au lieu de créer `fdec` simplifié

**AVANT (lignes 1489-1510):**
```python
fdec = {
    "ok": bool(out.get("ok")),
    "action": out.get("action"),
    "score": float(_score100),
    "price": (
        trig.get("anchor_price")
        if isinstance(trig, dict)
        else None
    ),
    "ttl_ms": ttl_ms,
    "slippage_guard_points": slippage_pts,
    "ts_created": __import__("pandas")
    .Timestamp.utcnow()
    .value
    // 1_000_000,
    "meta": {
        "signal_type": out.get("signal_type"),
        "consensus": out.get("consensus"),
        "trail": out.get("suggested_trailing"),
        "quality": out.get("quality"),
    },
}
```

**APRÈS (lignes 1489-1507):**
```python
# ✅ CORRECTION: Garder 'out' complet (avec components, trigger_boost, etc.)
# puis ajouter les champs supplémentaires nécessaires
fdec = dict(out)  # Copie de 'out' pour garder tous les champs FusionManager

# Ajouter/surcharger les champs de configuration
fdec.update({
    "score": float(_score100),
    "price": (
        trig.get("anchor_price")
        if isinstance(trig, dict)
        else None
    ) if "price" not in fdec or not fdec["price"] else fdec["price"],
    "ttl_ms": ttl_ms,
    "slippage_guard_points": slippage_pts,
    "ts_created": __import__("pandas")
    .Timestamp.utcnow()
    .value
    // 1_000_000,
})
```

---

## 🔍 Explication du Problème

### Flux de Données

1. **FusionManager.fuse()** retourne `out` avec **TOUS** les champs:
   - `fused_confidence` (score final)
   - `components` (orderflow, validator, trigger avec leurs scores)
   - `trigger_boost` (bonus trigger)
   - `consensus` (unanimité, votes)
   - `quality` (statut, warnings)
   - etc.

2. **Ancien code:** Créait `fdec` en copiant **seulement quelques champs** de `out`
   → Perte de `components`, `trigger_boost`, etc.

3. **Nouveau code:** Copie **tout** `out` dans `fdec`, puis ajoute les champs config
   → Garde tous les champs FusionManager

4. **Résultat:** `fdec` → `fusion_full` → `td["fusion_data"]` → TradeLogger
   → Les scores apparaissent dans trades_history.jsonl

---

## 📊 Résultat Attendu

### Avant (sur VPS actuellement):

**fusion_data vide ou incomplet:**
```json
{
  "fusion_data": {
    "ok": true,
    "action": "SELL",
    "score": 69.70,
    "price": 4137.16,
    "ttl_ms": 800,
    "slippage_guard_points": 10.0,
    "ts_created": 1732554700381,
    "meta": {
      "signal_type": "CAUTIOUS_SELL",
      "consensus": {...},
      "quality": {...}
    }
  }
}
```

❌ **Manque:** `components`, `trigger_boost`, `fused_confidence`

**Résultat dans trades_history.jsonl:**
```json
{
  "score_final": 0.0,
  "score_of": 0.0,
  "score_fp": 0.0,
  "trigger_boost": 0.0,
  "trigger_type": "none"
}
```

### Après déploiement:

**fusion_data complet:**
```json
{
  "fusion_data": {
    "ok": true,
    "action": "SELL",
    "fused_confidence": 0.697,
    "components": {
      "orderflow": {"score": 0.190, "status": "SUSPECT", "raw": {...}},
      "validator": {"score": 0.800, "status": "VALID", "raw": {...}},
      "trigger": {"score": 0.917, "type": "absorption_reject", "raw": {...}}
    },
    "trigger_boost": 0.202,
    "consensus": {
      "maj": "SELL",
      "agreement": 1.0,
      "votes": [...]
    },
    "quality": {
      "is_valid": true,
      "quality_score": 1.0,
      "warnings": [...]
    },
    "score": 69.70,
    "price": 4137.16,
    "ttl_ms": 800,
    "slippage_guard_points": 10.0,
    "ts_created": 1732554700381
  }
}
```

✅ **Contient:** Tous les champs

**Résultat dans trades_history.jsonl:**
```json
{
  "score_final": 0.697,
  "score_of": 0.190,
  "score_fp": 0.800,
  "trigger_boost": 0.202,
  "trigger_type": "absorption_reject",
  "status_of": "SUSPECT",
  "status_fp": "VALID",
  "tick_count": 97,
  "coverage_s": 37.0
}
```

---

## 🔄 Procédure de Déploiement

### 1. Copier le fichier modifié sur le VPS:
- `run_bot.py`

### 2. Supprimer le cache Python:
```powershell
# Sur Windows VPS
Get-ChildItem -Path . -Recurse -Filter "*.pyc" | Remove-Item -Force
Get-ChildItem -Path . -Recurse -Filter "__pycache__" | Remove-Item -Recurse -Force
```

Ou si vous n'avez pas PowerShell:
```cmd
del /s /q *.pyc
for /d /r %i in (__pycache__) do @if exist "%i" rd /s /q "%i"
```

### 3. Redémarrer le bot

### 4. Vérifier le prochain trade:

Ouvrir `trades_history.jsonl` et vérifier que les champs suivants ont des valeurs réelles:
- `score_final` (ex: 0.697 au lieu de 0.0)
- `score_of` (ex: 0.190 au lieu de 0.0)
- `score_fp` (ex: 0.800 au lieu de 0.0)
- `trigger_boost` (ex: 0.202 au lieu de 0.0)
- `trigger_type` (ex: "absorption_reject" au lieu de "none")
- `status_of` (ex: "SUSPECT" au lieu de "UNKNOWN")
- `status_fp` (ex: "VALID" au lieu de "UNKNOWN")

---

## ⚠️ Note sur la Latence

Les logs montrent que la latence entre positions n'a **pas diminué** (~110-120ms au lieu de 5ms).

**Cause possible:** Le fichier `trader/burst.py` n'a pas été correctement copié sur le VPS.

**Vérification sur VPS:**

Chercher la ligne avec le sleep dans burst.py:
```powershell
Select-String -Path "trader/burst.py" -Pattern "0.005"
```

**Attendu:** Devrait trouver:
```python
_t.sleep(float(self.config_manager.get("burst_send_sleep_s", 0.005) or 0.005))
```

Si vous voyez `0.01` au lieu de `0.005`, re-copier `trader/burst.py`.

---

## 📝 Fichiers à Déployer

1. **run_bot.py** (correction scores) ← **PRIORITAIRE**
2. **trader/burst.py** (latence - optionnel)

---

## ✅ Tests à Effectuer

Après redémarrage, attendre 1 trade et vérifier:

1. **Scores corrects dans JSONL:**
   ```
   tail -1 trades_history.jsonl
   ```
   Doit montrer `score_final` > 0.0

2. **Scores corrects dans Markdown:**
   ```
   tail -20 trades_history.md
   ```
   Doit montrer un tableau avec scores réels

3. **Latence (si burst.py déployé):**
   - Observer les timestamps dans la console
   - Les 8 positions devraient être espacées de ~15ms au lieu de ~120ms

---

Tout est prêt pour le déploiement !
