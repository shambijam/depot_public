# 📊 Analyse des Performances par Phase de Marché

## Vue d'ensemble

Ce système permet d'analyser les performances de trading selon les différentes phases de marché détectées. Il aide à identifier quels régimes et phases sont les plus profitables pour optimiser le scoring adaptatif VWAP.

## Fichiers

### 1. `trader/trade_logger.py`
Enregistre automatiquement chaque trade avec:
- **Phase Optimisée** (`market_phase`): Phase détectée (ex: `liquidity_sweep`, `trending_institutional_bull`)
- **Régime PhaseObserver** (`market_regime`): Régime parmi les 19 détectés (ex: `trending_institutional_bull`, `breakout_bull`)
- **Régime VWAP** (`vwap_regime`): `TRENDING`, `ACCUMULATION`, `BALANCED`, `TRANSITIONAL`
- **Confiance Phase** (`phase_confidence`): Score de confiance de la détection (0.0-1.0)
- **Score VWAP** (`vwap_score`): Score VWAP individuel (0.0-1.0)

### 2. `tools/analyze_by_market_phase.py`
Script d'analyse qui génère:
- Statistiques WIN/LOSS par régime VWAP
- Statistiques WIN/LOSS par régime PhaseObserver
- Statistiques WIN/LOSS par phase optimisée
- PnL moyen par phase
- Recommandations d'optimisation

## Utilisation

### Lancer l'analyse
```bash
cd /home/workdev/sniper_x_dev
python3 tools/analyze_by_market_phase.py
```

### Output

#### Console
Affiche un résumé rapide:
```
================================================================================
📊 ANALYSE DE PERFORMANCE PAR PHASE DE MARCHÉ
================================================================================

Total trades analysés: 145

📈 RÉGIMES VWAP (triés par Win Rate):
--------------------------------------------------------------------------------
  TRENDING             | Trades:  82 | Win Rate:  68.3% | PnL Moy:  +12.5 pips | Score:  75.2%
  BALANCED             | Trades:  35 | Win Rate:  54.3% | PnL Moy:   +5.8 pips | Score:  68.4%
  ACCUMULATION         | Trades:  20 | Win Rate:  45.0% | PnL Moy:   -2.1 pips | Score:  63.1%
  TRANSITIONAL         | Trades:   8 | Win Rate:  37.5% | PnL Moy:   -8.3 pips | Score:  58.9%
```

#### Fichier Markdown
Génère `logs/performance_by_phase.md` avec:
- Tableaux détaillés par phase
- Graphiques de performance
- Recommandations d'optimisation

## Exemple de Rapport

```markdown
# 📊 Analyse de Performance par Phase de Marché

## 📈 Performance par Régime VWAP

| Régime VWAP | Trades | Win Rate | PnL Moy (pips) | PnL Moy (USD) | Score Moy | W/L/BE |
|-------------|--------|----------|----------------|---------------|-----------|--------|
| 📈 **TRENDING** | 82 | 68.3% | +12.5 | +125.00 | 75.2% | 56/22/4 |
| ⚖️ **BALANCED** | 35 | 54.3% | +5.8 | +58.00 | 68.4% | 19/14/2 |
| 📊 **ACCUMULATION** | 20 | 45.0% | -2.1 | -21.00 | 63.1% | 9/10/1 |
| 🔄 **TRANSITIONAL** | 8 | 37.5% | -8.3 | -83.00 | 58.9% | 3/5/0 |

## 💡 Recommandations

### 📈 Régimes VWAP

- ✅ **Meilleur régime**: TRENDING (Win Rate: 68.3%, 82 trades)
- ❌ **Pire régime**: TRANSITIONAL (Win Rate: 37.5%, 8 trades)

💡 **Action suggérée**: Augmenter le poids VWAP en régime TRENDING
⚠️ **Action suggérée**: Réduire le poids VWAP en régime TRANSITIONAL ou filtrer les trades
```

## Interprétation des Résultats

### Métriques Clés

1. **Win Rate**: % de trades gagnants
   - `> 60%` = Excellent
   - `50-60%` = Bon
   - `< 50%` = À améliorer

2. **PnL Moyen (pips)**: Profit/Perte moyen par trade
   - `> +10 pips` = Très profitable
   - `+5 to +10 pips` = Profitable
   - `< +5 pips` = Marginal

3. **Score Moyen**: Score de confiance moyen
   - Identifie si les bons scores correspondent aux bonnes phases

### Actions d'Optimisation

#### Si un régime VWAP a Win Rate > 65%
→ **Augmenter le poids VWAP** dans `config/vwap_adaptive_config.json`

Exemple:
```json
"TRENDING": {
  "weights": {
    "vwap": 0.55,  // Augmenter de 50% → 55%
    "orderflow": 0.27,
    "footprint": 0.18
  }
}
```

#### Si un régime VWAP a Win Rate < 45%
→ **Réduire le poids VWAP** ou **filtrer les trades**

Exemple:
```json
"TRANSITIONAL": {
  "weights": {
    "vwap": 0.15,  // Réduire de 20% → 15%
    "orderflow": 0.45,
    "footprint": 0.40
  }
}
```

#### Si un régime PhaseObserver spécifique est très profitable
→ **Créer un cas spécial** dans `vwap_adaptive_config.json`

Exemple:
```json
"special_cases": {
  "BREAKOUT": {
    "weight_multiplier": 1.20,  // Boost +20%
    "window_multiplier": 0.80
  }
}
```

## Workflow d'Optimisation

1. **Collecte**: Laisser tourner le bot pendant 1-2 semaines
2. **Analyse**: Exécuter `analyze_by_market_phase.py`
3. **Identification**: Repérer les phases avec meilleur/pire Win Rate
4. **Ajustement**: Modifier `vwap_adaptive_config.json`
5. **Test**: Observer l'impact sur 1 semaine
6. **Itération**: Répéter le cycle

## Fichiers de Log

### `logs/trades_history.jsonl`
Format JSON Lines (1 trade par ligne), parfait pour analyse automatique:
```json
{
  "trade_id": "abc12345",
  "symbol": "XAUUSD",
  "direction": "BUY",
  "outcome": "WIN",
  "pnl_pips": 15.2,
  "market_phase": "trending_institutional_bull",
  "market_regime": "trending_institutional_bull",
  "vwap_regime": "TRENDING",
  "phase_confidence": 0.82,
  "vwap_score": 0.78,
  ...
}
```

### `logs/trades_history.md`
Format Markdown lisible par humain avec toutes les infos détaillées.

## Exemple Complet

```bash
# 1. Le bot trade pendant 1 semaine
python3 run_bot.py

# 2. Analyser les résultats
python3 tools/analyze_by_market_phase.py

# 3. Consulter le rapport
cat logs/performance_by_phase.md

# 4. Si TRENDING a 70% Win Rate → Augmenter VWAP weight
nano config/vwap_adaptive_config.json

# 5. Relancer et observer
python3 run_bot.py
```

## Notes Importantes

- Les données de phase sont **automatiquement** enregistrées à chaque trade
- L'analyse nécessite au moins **20-30 trades** pour être significative
- Les phases rares (< 5 trades) peuvent avoir des stats non représentatives
- Toujours **valider les changements sur période de test** avant production

## Maintenance

- Les logs sont dans `logs/trades_history.jsonl`
- Nettoyage recommandé tous les 3 mois (archivage)
- Garder au moins 200-300 trades pour analyse statistique valide

---

*Dernière mise à jour: 06 DEC 2025*
*Système de scoring VWAP dynamique*
