# FIX - AttributeError StrategyManager ✅

**Date** : 2 Décembre 2025
**Impact** : 🔴 CRITIQUE - Erreur bloquante
**Statut** : ✅ CORRIGÉ

---

## 🔴 ERREUR

```python
AttributeError: 'StrategyManager' object has no attribute 'strategies'
```

**Ligne** : `run_bot.py:1366`

```python
scalping_strategy = strategy_manager.strategies.get("scalping")  # ❌ FAUX
```

---

## 🔍 ANALYSE

### Erreur dans le Code

J'ai utilisé `strategy_manager.strategies.get("scalping")` en pensant que `StrategyManager` avait un attribut `strategies` (dict).

**MAIS** : `StrategyManager` expose les stratégies via une **méthode** :
- ✅ `get_strategy_instance(name)` → Retourne l'instance de la stratégie
- ❌ `strategies` (attribut) → N'existe pas

### Preuve dans decision_pipeline.py

**Ligne 316** :
```python
scalping = self.strategy_manager.get_strategy_instance("scalping")  # ✅ CORRECT
```

**Ligne 432** :
```python
liquidity = self.strategy_manager.get_strategy_instance("liquidity")  # ✅ CORRECT
```

---

## ✅ SOLUTION

### Modification run_bot.py ligne 1366

**AVANT (incorrect)** :
```python
scalping_strategy = strategy_manager.strategies.get("scalping")  # ❌
```

**APRÈS (correct)** :
```python
scalping_strategy = strategy_manager.get_strategy_instance("scalping")  # ✅
```

---

## 📊 RÉSULTAT ATTENDU

### Avant Fix (Logs)

```
[ERROR] - [OF V6][XAUUSD] Erreur calcul OrderFlow V6: 'StrategyManager' object has no attribute 'strategies'
[INFO] - [SIMPLE_SCORE] OF=0.000 FP=0.900 base=0.450 | status_of=ERROR  ❌
```

### Après Fix (Logs attendus)

```
[INFO] - [OF V6][XAUUSD] ✅ Score calculé: 60.0/100 (30.0/50 pts) | Status=VALID | Bias=BUY
[INFO] - [SIMPLE_SCORE] OF=0.600 FP=0.900 base=0.750 | status_of=VALID  ✅
```

---

## 🎯 VALIDATION

### Syntaxe Python
```bash
python3 -c "import ast; ast.parse(open('run_bot.py').read())"
# → ✅ Aucune erreur
```

### Test Runtime (à faire)
1. Relancer le bot
2. Vérifier logs : `grep "\[OF V6\]" logs/bot.log`
3. Vérifier que `status_of != ERROR`

---

## 📝 LEÇON APPRISE

**Toujours vérifier l'API d'une classe avant de l'utiliser !**

- ✅ Regarder les méthodes publiques dans la définition de classe
- ✅ Chercher des exemples d'utilisation dans le code existant
- ❌ Ne pas supposer qu'un attribut existe sans vérifier

---

*Fix appliqué le 2 Décembre 2025 - 16:15*
