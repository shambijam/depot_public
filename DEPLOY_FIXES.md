# 🚀 Correctifs à Déployer sur VPS

**Date:** 2025-11-25
**Objectif:** Corriger les scores à 0% dans trades_history.jsonl

---

## ✅ Déjà Déployé

- **trader/trade_logger.py** - Écriture immédiate des trades (26 trades enregistrés avec succès)

---

## 📦 À Déployer (2 fichiers)

### 1. phase_observer/fusion_manager.py

**3 modifications:**

**Ligne 629** - Décomposer le retour de `_calculate_fused_confidence()`:
```python
# AVANT:
fused = self._calculate_fused_confidence(
    n_of, n_fp, n_tr, coherence, quality, cfg, ctx, rules_eval
)

# APRÈS:
fused, trigger_boost = self._calculate_fused_confidence(
    n_of, n_fp, n_tr, coherence, quality, cfg, ctx, rules_eval
)
```

**Ligne 710** - Ajouter `trigger_boost` au dictionnaire retourné:
```python
return {
    "ok": is_actionable,
    "action": decision["action"],
    "signal_type": decision["signal_type"],
    "direction": decision["direction"],
    "fused_confidence": round(float(fused), 3),
    "trigger_boost": round(float(trigger_boost), 3),  # ✅ AJOUTER CETTE LIGNE
    "anchor_price": decision["anchor_price"],
    "rationale": rationale,
    "components": {"orderflow": n_of, "validator": n_fp, "trigger": n_tr},
    "consensus": {
        "maj": maj_str,
        "agreement": round(coherence["agreement"], 3),
        "votes": coherence["votes"],
    },
    "quality": quality,
}
```

**Ligne 1303** - Retourner tuple au lieu de simple float:
```python
# AVANT:
return final_score

# APRÈS:
# Retourner (score final, trigger_boost) pour que burst.py puisse logger le boost
return (final_score, trigger_boost)
```

---

### 2. trader/burst.py

**4 modifications dans la section de logging (lignes 112-130):**

**Ligne 112** - Lire directement trade_decision:
```python
# AVANT:
fusion_data = td.get("fusion_data", {})

# APRÈS:
fusion_data = td  # td contient déjà fused_confidence, components, consensus, quality
```

**Ligne 117** - Lire components:
```python
# AVANT:
normalized = fusion_data.get("normalized", {})

# APRÈS:
components = fusion_data.get("components", {})
```

**Lignes 119-120** - Lire depuis components:
```python
# AVANT:
n_of = normalized.get("orderflow", {})
n_fp = normalized.get("footprint", {})

# APRÈS:
n_of = components.get("orderflow", {})
n_fp = components.get("validator", {})  # FusionManager retourne "validator" pas "footprint"
```

**Ligne 130** - trigger_boost maintenant disponible:
```python
# Pas de changement au code, mais maintenant cette ligne fonctionnera:
trigger_boost = float(fusion_data.get("trigger_boost", 0.0))
```

---

## 🔄 Procédure de Déploiement

1. **Copier les 2 fichiers sur le VPS:**
   - `phase_observer/fusion_manager.py`
   - `trader/burst.py`

2. **Redémarrer le bot**

3. **Vérifier le prochain trade:**
   - Ouvrir `trades_history.jsonl`
   - Vérifier que les champs suivants ont des valeurs réelles (pas 0.0):
     - `score_final` (ex: 0.461)
     - `score_of` (ex: 0.37)
     - `score_fp` (ex: 0.42)
     - `trigger_boost` (ex: 0.120)
     - `status_of` (VALID ou SUSPECT)
     - `status_fp` (VALID ou SUSPECT)
     - `tick_count` (ex: 847)
     - `coverage_s` (ex: 3.2)

---

## 📊 Résultat Attendu

**Avant (VPS actuel):**
```json
{
  "score_final": 0.0,
  "score_of": 0.0,
  "score_fp": 0.0,
  "trigger_boost": 0.0,
  "status_of": "UNKNOWN",
  "status_fp": "UNKNOWN"
}
```

**Après déploiement:**
```json
{
  "score_final": 0.461,
  "score_of": 0.37,
  "score_fp": 0.42,
  "trigger_boost": 0.120,
  "status_of": "SUSPECT",
  "status_fp": "SUSPECT",
  "tick_count": 847,
  "coverage_s": 3.2
}
```

---

## ⚠️ Important

- **NE PAS commit** (vous gérez Git manuellement)
- Les 26 trades existants dans le JSONL garderont leurs scores à 0% (c'est normal)
- Seuls les **nouveaux trades** après déploiement auront les vrais scores
- Le fichier `trades_history.md` sera aussi corrigé automatiquement
