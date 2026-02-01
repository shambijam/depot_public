# RÉCAPITULATIF SESSION - 23 JANVIER 2026

## 🎯 PROBLÈME INITIAL

Le paramètre `target_profit_pips` dans `closure_rules` ne fonctionnait plus après la refactorisation majeure du code. Bug présent depuis plusieurs jours malgré de multiples tentatives de correction.

---

## 🔍 ANALYSE EFFECTUÉE

### Comparaison des versions

| Élément | Version qui MARCHE (13 jan) | Version CASSÉE (backup) |
|---------|----------------------------|------------------------|
| `run_bot.py` | 5118 lignes (monolithique) | 80 lignes (stub) |
| Dossier `orchestration/` | N'existe PAS | Présent avec workers |
| `closure_rules` | Fonctionne | Ignoré (défauts hardcodés) |

### Structure des fichiers JSON (assets_config/)

```
overrides:
  scalping:
    closure_rules:           ← ICI est le closure_rules dans les JSON
      target_profit_pips: X
      max_loss_pips: Y
      ...
```

### Valeurs par asset:
- **USDJPY**: `target_profit_pips: 1.23`
- **GBPUSD**: `target_profit_pips: 1.6`
- **NAS100**: `target_profit_pips: 5.0`

---

## 🐛 CAUSE RACINE IDENTIFIÉE

### Dans `trader/burst.py` - fonction `monitor_burst_baskets()`

**VERSION QUI MARCHE (13 janvier) - Logique simple:**
```python
# Ligne ~862 - Lecture directe
closure = burst_cfg.get("closure_rules", {}) or {}

# Ligne ~878 - Utilisation directe
target_profit_pips = float(closure.get("target_profit_pips", 15.0))
```

**VERSION CASSÉE - Logique complexe qui échoue:**
```python
# burst_cfg.get("closure_rules", {}) retourne {} VIDE
# car après fusion ConfigMerger, closure_rules est à la RACINE, pas dans entry_rules.scalping.burst_scalping

config_closure = burst_cfg.get("closure_rules", {}) or {}  # ← VIDE!

closure = {
    "enabled": True,
    "target_profit_pips": 15.0,  # ← Défauts hardcodés utilisés
    ...
}
closure.update(config_closure)  # ← Update avec dict VIDE = aucun effet!
```

### Pourquoi le bug?

La refactorisation de `run_bot.py` vers le dossier `orchestration/` a changé **comment la config est passée** à `monitor_burst_baskets()`:

1. **Avant (monolithique)**: Le code préparait explicitement `closure_cfg` depuis le bon chemin
2. **Après (orchestration)**: Le worker passe la config différemment, `closure_rules` n'est plus au même endroit dans le dict

---

## ✅ SOLUTION APPLIQUÉE

**Revert au commit du 13 janvier** qui utilise le `run_bot.py` monolithique (5118 lignes).

Le `target_profit_pips` fonctionne de nouveau correctement avec cette version.

---

## 📁 FICHIERS DE RÉFÉRENCE

Le dossier `backup_broken/` a été conservé dans `/home/workdev/sniper_x_dev/` pour référence future. Il contient:
- `orchestration/` - Le dossier refactorisé qui cause le bug
- `run_bot.py` - Version 80 lignes (stub)
- `main.py` - Point d'entrée refactorisé

---

## 🤔 RÉFLEXIONS POUR LA SUITE

### Option 1: Garder le monolithe
- ✅ Fonctionne maintenant
- ❌ Fichier de 5118 lignes difficile à maintenir

### Option 2: Refactorisation propre
- ✅ Code plus maintenable
- ❌ Risque de recréer le bug
- ⚠️ Ne PAS modifier `burst.py` - adapter l'orchestration pour passer la config correctement

### Point clé à retenir:
Le problème n'est pas dans `burst.py` lui-même, mais dans **comment la config lui est transmise** par le layer orchestration. Une refactorisation propre devrait s'assurer que `burst_cfg.get("closure_rules", {})` retourne bien les valeurs du JSON, pas un dict vide.

---

## 📋 COMMITS DE RÉFÉRENCE

- **Version qui marche**: Commit du 13 janvier 2026
- **Version actuelle**: `prod` branch, commit `78bd776` (Scalping_TEST_refacto_config_merge_overrides_28)

---

## ⏰ PROCHAINES ÉTAPES

L'utilisateur souhaite réfléchir ce week-end à tête reposée pendant que le marché est fermé. Décision à prendre:
1. Rester sur le monolithe
2. Tenter une nouvelle refactorisation (sans toucher à burst.py)

---

*Document généré le 23 janvier 2026*
