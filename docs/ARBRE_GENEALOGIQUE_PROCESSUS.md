# ARBRE GÉNÉALOGIQUE DES PROCESSUS - SNIPER_X BOT

## Document de référence complet du pipeline de trading
**Version:** 1.0
**Date:** 6 Décembre 2025
**Auteur:** Architecture Team

---

## TABLE DES MATIÈRES

1. [Vue d'ensemble](#1-vue-densemble)
2. [Démarrage du Bot](#2-démarrage-du-bot)
3. [Cycle de Trading Principal](#3-cycle-de-trading-principal)
4. [Pipeline d'Analyse de Marché](#4-pipeline-danalyse-de-marché)
5. [Pipeline de Décision](#5-pipeline-de-décision)
6. [Pipeline d'Exécution](#6-pipeline-dexécution)
7. [Cycle de Vie d'un Trade Complet](#7-cycle-de-vie-dun-trade-complet)
8. [Modules de Support](#8-modules-de-support)
9. [Gestion des Erreurs et Récupération](#9-gestion-des-erreurs-et-récupération)
10. [Logs et Audit Trail](#10-logs-et-audit-trail)

---

## 1. VUE D'ENSEMBLE

### 1.1 Architecture Globale

```
┌────────────────────────────────────────────────────────────────────┐
│                         SNIPER_X BOT                               │
│                    Trading System Architecture                     │
└────────────────────────────────────────────────────────────────────┘
                                  │
                    ┌─────────────┴─────────────┐
                    ▼                           ▼
        ┌───────────────────┐       ┌───────────────────┐
        │   INITIALISATION  │       │   BOUCLE TRADING  │
        │   (main.py)       │──────▶│   (run_bot.py)    │
        └───────────────────┘       └───────────────────┘
                                             │
                    ┌────────────────────────┼────────────────────────┐
                    ▼                        ▼                        ▼
          ┌─────────────────┐    ┌──────────────────┐    ┌──────────────────┐
          │  MARKET DATA    │    │    DECISION      │    │   EXECUTION      │
          │  COLLECTION     │───▶│    PIPELINE      │───▶│   & MONITORING   │
          └─────────────────┘    └──────────────────┘    └──────────────────┘
                    │                      │                        │
                    ▼                      ▼                        ▼
          ┌─────────────────┐    ┌──────────────────┐    ┌──────────────────┐
          │ - MT5Connector  │    │ - MarketAnalyzer │    │ - TradeExecutor  │
          │ - DataEngine    │    │ - FusionManager  │    │ - OrderBuilder   │
          │ - Footprint M1  │    │ - Strategies     │    │ - Risk Controls  │
          └─────────────────┘    └──────────────────┘    └──────────────────┘
```

### 1.2 Principes Architecturaux

- **Singleton Pattern**: ConfigManager, MT5Connector (une seule instance)
- **Pipeline Pattern**: Data → Analysis → Decision → Execution
- **Strategy Pattern**: Stratégies interchangeables (Scalping, Liquidity)
- **Observer Pattern**: PhaseObserver surveille les changements de marché
- **Factory Pattern**: StrategyManager crée les instances de stratégies
- **Validator Pattern**: Validations à chaque étape du pipeline

---

## 2. DÉMARRAGE DU BOT

### 2.1 Séquence de Démarrage Complète

```
┌─────────────────────────────────────────────────────────────────────────┐
│ PHASE 0: ENTRY POINT                                                    │
│ Fichier: cli.py / main.py                                               │
└─────────────────────────────────────────────────────────────────────────┘
                                   │
                                   ▼
┌─────────────────────────────────────────────────────────────────────────┐
│ PHASE 1: CONFIGURATION INITIALE                                         │
│ Fichier: main.py::main()                                                │
├─────────────────────────────────────────────────────────────────────────┤
│ 1.1 Chargement des variables d'environnement (.env)                     │
│     - TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID                              │
│     - MT5_LOGIN, MT5_PASSWORD, MT5_SERVER                               │
│     - SNIPERX_DEFAULT_EQUITY, MODE_EXECUTION                            │
│                                                                          │
│ 1.2 Configuration du Logging                                            │
│     utils/logger_setup.py::setup_production_logging()                   │
│     - Création fichier logs/sniper_x_{date}.log                         │
│     - Niveau: INFO/DEBUG selon args.log_level                           │
│     - Formatage: timestamp + module + level + message                   │
│                                                                          │
│ 1.3 Initialisation ConfigManager (SINGLETON)                            │
│     core/config_manager.py::ConfigManager()                             │
│     - Chargement config/prod_config.json                                │
│     - Validation schémas JSON                                           │
│     - Cache runtime des configurations                                  │
└─────────────────────────────────────────────────────────────────────────┘
                                   │
                                   ▼
┌─────────────────────────────────────────────────────────────────────────┐
│ PHASE 2: INITIALISATION DES COMPOSANTS CORE                             │
│ Fichier: main.py::main() (lignes 260-280)                               │
├─────────────────────────────────────────────────────────────────────────┤
│ 2.1 AuditLogger                                                          │
│     core/audit_logger.py::AuditLogger()                                 │
│     - Création fichier logs/audit_{date}.log                            │
│     - Trail toutes actions (config, trades, erreurs)                    │
│                                                                          │
│ 2.2 MT5Connector (SINGLETON)                                            │
│     mt5_connector.py::MT5Connector()                                    │
│     - Initialisation library MetaTrader5                                │
│     - Récupération credentials depuis ConfigManager                     │
│     - Tentative connexion au broker                                     │
│     ├─ Mode DEMO: Compte MT5 démo                                       │
│     └─ Mode LIVE: Compte MT5 réel (WARNING critique)                    │
│                                                                          │
│ 2.3 StrategyManager                                                      │
│     core/strategy_manager.py::StrategyManager()                         │
│     - Chargement dynamique des stratégies                               │
│     - Scan config/strategy/*.json                                       │
│     - Import classes Python strategy/*.py                               │
│     - Enregistrement dans strategy_registry                             │
│         ├─ ScalpingStrategy (magic: 52001)                              │
│         └─ LiquidityStrategy (magic: 53001)                             │
│                                                                          │
│ 2.4 DecisionPipeline                                                     │
│     core/decision_pipeline.py::DecisionPipeline()                       │
│     - Initialisation MarketAnalyzer                                     │
│     - Chargement configs assets (EURUSD, GBPUSD, XAUUSD)                │
│     - Création cache pour performances                                  │
│                                                                          │
│ 2.5 Mecano (Diagnostics)                                                │
│     mecanique_generale/mecano.py::Mecano()                              │
│     - Monitoring système (CPU, RAM, Disk)                               │
│     - Health checks périodiques                                         │
│                                                                          │
│ 2.6 PhaseObserver                                                        │
│     phase_observer/orchestrator.py::PhaseObserver()                     │
│     - Initialisation des 8 détecteurs                                   │
│     - Chargement FeaturesExtractor                                      │
│     - Création PhaseMemoryManager                                       │
│                                                                          │
│ 2.7 MarketAnalyzer                                                       │
│     phase_observer/market_analyzer.py::MarketAnalyzer()                 │
│     - Chargement FootprintAnalyzer                                      │
│     - Chargement VWAPAnalyzer                                           │
│     - Initialisation FusionManager                                      │
└─────────────────────────────────────────────────────────────────────────┘
                                   │
                                   ▼
┌─────────────────────────────────────────────────────────────────────────┐
│ PHASE 3: VÉRIFICATIONS PRÉ-DÉMARRAGE                                    │
│ Fichier: main.py::verify_environment_and_config() (lignes 76-158)      │
├─────────────────────────────────────────────────────────────────────────┤
│ 3.1 Vérification Configuration Dynamique                                │
│     - config_manager.get_current_dynamic_config() non vide              │
│     - Schéma JSON valide                                                │
│                                                                          │
│ 3.2 Vérification Compte MT5                                             │
│     - config_manager.get_mt5_account_credentials(mode)                  │
│     - Login, Password, Server présents                                  │
│     - allowed_symbols non vide                                          │
│                                                                          │
│ 3.3 Vérification Telegram                                               │
│     - TELEGRAM_BOT_TOKEN présent                                        │
│     - TELEGRAM_CHAT_ID présent                                          │
│     - Test connexion (si activé)                                        │
│                                                                          │
│ 3.4 Connexion MT5 Persistante                                           │
│     mt5_connector.connect(account_details)                              │
│     - Login au broker                                                   │
│     - Vérification account_info (balance, equity)                       │
│     - Symboles autorisés disponibles                                    │
│                                                                          │
│ 3.5 TradeExecutor                                                        │
│     trader/trade_executor.py::TradeExecutor()                           │
│     - Mode: DEMO ou LIVE (depuis config_manager)                        │
│     - Initialisation modules internes:                                  │
│         ├─ OrderBuilder                                                 │
│         ├─ Sizing                                                       │
│         ├─ SLTP                                                         │
│         ├─ BurstHandler                                                 │
│         ├─ Validators                                                   │
│         └─ TradeLogger                                                  │
│     - Réconciliation état avec broker                                   │
│       trade_executor.reconcile_state_with_broker()                      │
│         ├─ Récupération positions MT5                                   │
│         ├─ Récupération ordres en attente                               │
│         └─ Mise à jour cache interne                                    │
└─────────────────────────────────────────────────────────────────────────┘
                                   │
                                   ▼
┌─────────────────────────────────────────────────────────────────────────┐
│ PHASE 4: NOTIFICATION DÉMARRAGE                                         │
├─────────────────────────────────────────────────────────────────────────┤
│ 4.1 Alerte Telegram                                                     │
│     config_manager.send_alert("SNIPER_X Bot Démarré!")                  │
│     - Message: Mode (DEMO/LIVE), timestamp, compte MT5                  │
│                                                                          │
│ 4.2 Logs Critiques                                                      │
│     logger.critical("SNIPER_X Bot prêt. Mode: {mode}")                  │
│     logger.info("Compte MT5: {account_id}, Balance: {balance}")         │
│                                                                          │
│ 4.3 Snapshot Configuration Burst                                        │
│     - Log burst_size depuis prod_config.json                            │
│     - Log burst_size depuis config_trade_scalping.json                  │
│     - Log burst_size depuis XAUUSD.json (overrides)                     │
│     - Détection conflits de configuration                               │
└─────────────────────────────────────────────────────────────────────────┘
                                   │
                                   ▼
┌─────────────────────────────────────────────────────────────────────────┐
│ PHASE 5: ALIGNEMENT HORLOGE SUISSE                                      │
│ Fichier: main.py::sleep_until_next_minute() (ligne 383)                │
├─────────────────────────────────────────────────────────────────────────┤
│ - Calcul temps jusqu'à prochaine minute exacte                          │
│ - time.sleep() pour synchronisation                                     │
│ - Premier cycle démarre à la minute 00 secondes                         │
└─────────────────────────────────────────────────────────────────────────┘
                                   │
                                   ▼
                    ┌──────────────────────────────┐
                    │ ENTRÉE BOUCLE TRADING (∞)   │
                    │ main.py (lignes 386-503)    │
                    └──────────────────────────────┘
```

### 2.2 Composants Initialisés - Détail

| Composant | Fichier | Rôle | Dépendances |
|-----------|---------|------|-------------|
| **ConfigManager** | `core/config_manager.py` | Singleton, gestion centralisée configs | ConfigLoader, AuditLogger |
| **MT5Connector** | `mt5_connector.py` | Singleton, connexion broker MT5 | MetaTrader5 lib |
| **StrategyManager** | `core/strategy_manager.py` | Chargement dynamique stratégies | ConfigManager, ConfigLoader |
| **DecisionPipeline** | `core/decision_pipeline.py` | Orchestration décisions trading | ConfigManager, StrategyManager, MarketAnalyzer |
| **MarketAnalyzer** | `phase_observer/market_analyzer.py` | Analyse marché unifiée | PhaseObserver, FootprintAnalyzer, VWAPAnalyzer, FusionManager |
| **PhaseObserver** | `phase_observer/orchestrator.py` | Détection phases marché | FeaturesExtractor, Detectors (x8), PhaseMemoryManager |
| **TradeExecutor** | `trader/trade_executor.py` | Exécution trades | MT5Connector, ConfigManager, OrderBuilder, Sizing, SLTP |
| **AuditLogger** | `core/audit_logger.py` | Trail audit complet | ConfigManager |
| **Mecano** | `mecanique_generale/mecano.py` | Diagnostics système | ConfigManager |

---

## 3. CYCLE DE TRADING PRINCIPAL

### 3.1 Boucle Principale (While True)

```
┌─────────────────────────────────────────────────────────────────────────┐
│ CYCLE N (répété à chaque minute exacte)                                 │
│ Fichier: main.py (lignes 386-503)                                       │
│ Fonction: run_single_pipeline_cycle() depuis run_bot.py                 │
└─────────────────────────────────────────────────────────────────────────┘
                                   │
                    ┌──────────────┴──────────────┐
                    ▼                             ▼
        ┌───────────────────┐          ┌──────────────────┐
        │ ÉTAPE A:          │          │ ÉTAPE B:         │
        │ MARKET DATA       │─────────▶│ SIGNAL           │
        │ COLLECTION        │          │ GENERATION       │
        └───────────────────┘          └──────────────────┘
                                               │
                    ┌──────────────────────────┼──────────────────────────┐
                    ▼                          ▼                          ▼
        ┌───────────────────┐    ┌──────────────────┐    ┌──────────────────┐
        │ ÉTAPE C:          │    │ ÉTAPE D:         │    │ ÉTAPE E:         │
        │ DECISION          │───▶│ EXECUTION        │───▶│ MONITORING       │
        │ MAKING            │    │ & VALIDATION     │    │ & LOGGING        │
        └───────────────────┘    └──────────────────┘    └──────────────────┘
                                                                  │
                    ┌─────────────────────────────────────────────┘
                    ▼
        ┌───────────────────────────────────────────────────────┐
        │ ÉTAPE F: SURVEILLANCE & ACTIONS PÉRIODIQUES           │
        ├───────────────────────────────────────────────────────┤
        │ - Surveillance baskets burst (fermeture +15 pips)     │
        │ - Recalibration PhaseObserver (toutes les 30 min)    │
        │ - Envoi alertes Telegram (résumé compte)              │
        │ - Analyse live bar (pré-signal)                       │
        └───────────────────────────────────────────────────────┘
                                   │
                                   ▼
        ┌───────────────────────────────────────────────────────┐
        │ ÉTAPE G: SYNCHRONISATION HORLOGE                      │
        ├───────────────────────────────────────────────────────┤
        │ sleep_until_next_minute()                             │
        │ - Attente jusqu'à prochaine minute exacte (00s)       │
        │ - Garantit cycle toutes les 60 secondes précises      │
        └───────────────────────────────────────────────────────┘
                                   │
                                   └─────▶ LOOP BACK TO CYCLE N+1
```

### 3.2 Détail Étape A: MARKET DATA COLLECTION

```
┌─────────────────────────────────────────────────────────────────────────┐
│ A. MARKET DATA COLLECTION                                               │
│ Fichier: run_bot.py::run_single_pipeline_cycle()                        │
├─────────────────────────────────────────────────────────────────────────┤
│ A.1 Récupération Données OHLC (pour chaque actif: EURUSD, GBPUSD,      │
│     XAUUSD)                                                             │
│     ┌───────────────────────────────────────────────────────────┐      │
│     │ MT5Connector.get_market_data(symbol, timeframe, count)    │      │
│     ├───────────────────────────────────────────────────────────┤      │
│     │ - M1 (1 minute):  120 dernières bougies                   │      │
│     │ - M5 (5 minutes): 240 dernières bougies                   │      │
│     │ - M15 (15 min):   240 dernières bougies                   │      │
│     └───────────────────────────────────────────────────────────┘      │
│                                                                          │
│ A.2 Récupération Données Footprint M1                                   │
│     ┌───────────────────────────────────────────────────────────┐      │
│     │ MT5Connector.get_ticks(symbol, from_time, to_time)        │      │
│     ├───────────────────────────────────────────────────────────┤      │
│     │ - Ticks bruts sur fenêtre [current_minute-1, current]    │      │
│     │ - Bid/Ask/Volume/Flags pour chaque tick                   │      │
│     │ - Stockage pour FootprintAnalyzer                         │      │
│     └───────────────────────────────────────────────────────────┘      │
│                                                                          │
│ A.3 Récupération Account Info                                           │
│     ┌───────────────────────────────────────────────────────────┐      │
│     │ MT5Connector.get_account_info()                           │      │
│     ├───────────────────────────────────────────────────────────┤      │
│     │ - Balance (solde compte)                                  │      │
│     │ - Equity (capital actuel avec positions ouvertes)         │      │
│     │ - Margin (marge utilisée)                                 │      │
│     │ - Free Margin (marge disponible)                          │      │
│     │ - Profit (P&L non réalisé)                                │      │
│     └───────────────────────────────────────────────────────────┘      │
│                                                                          │
│ A.4 Récupération Positions Ouvertes                                     │
│     ┌───────────────────────────────────────────────────────────┐      │
│     │ MT5Connector.get_positions()                              │      │
│     ├───────────────────────────────────────────────────────────┤      │
│     │ - Liste positions actives (tickets, symbole, type)        │      │
│     │ - SL/TP/Entry/Current price                               │      │
│     │ - Profit/Volume de chaque position                        │      │
│     └───────────────────────────────────────────────────────────┘      │
│                                                                          │
│ A.5 Récupération Symbol Info                                            │
│     ┌───────────────────────────────────────────────────────────┐      │
│     │ MT5Connector.get_symbol_info(symbol)                      │      │
│     ├───────────────────────────────────────────────────────────┤      │
│     │ - Spread actuel (pips)                                    │      │
│     │ - Bid/Ask prix courants                                   │      │
│     │ - Point (taille pip)                                      │      │
│     │ - Digits (décimales)                                      │      │
│     │ - Volume min/max/step                                     │      │
│     └───────────────────────────────────────────────────────────┘      │
│                                                                          │
│ A.6 Construction Market Context                                         │
│     ┌───────────────────────────────────────────────────────────┐      │
│     │ DataEngine.build_market_context()                         │      │
│     ├───────────────────────────────────────────────────────────┤      │
│     │ - Agrégation OHLC multi-timeframes                        │      │
│     │ - Injection ticks footprint                               │      │
│     │ - Packaging account_info + symbol_info                    │      │
│     │ - Création dataframe prêt pour analyse                    │      │
│     └───────────────────────────────────────────────────────────┘      │
└─────────────────────────────────────────────────────────────────────────┘
```

---

## 4. PIPELINE D'ANALYSE DE MARCHÉ

### 4.1 Détail Étape B: SIGNAL GENERATION

```
┌─────────────────────────────────────────────────────────────────────────┐
│ B. SIGNAL GENERATION                                                    │
│ Fichier: phase_observer/market_analyzer.py::analyze()                  │
│ Appelé par: DecisionPipeline.run()                                     │
└─────────────────────────────────────────────────────────────────────────┘
                                   │
                    ┌──────────────┴──────────────┐
                    ▼                             ▼
        ┌───────────────────┐          ┌──────────────────┐
        │ B.1 PhaseObserver │          │ B.2 OrderFlow V6 │
        │ (Optionnel)       │          │ Analysis         │
        └───────────────────┘          └──────────────────┘
                    │                             │
                    ▼                             ▼
        ┌───────────────────┐          ┌──────────────────┐
        │ B.3 Footprint M1  │          │ B.4 VWAP         │
        │ Analysis          │          │ Analysis         │
        └───────────────────┘          └──────────────────┘
                    │                             │
                    └──────────────┬──────────────┘
                                   ▼
                    ┌──────────────────────────────┐
                    │ B.5 FusionManager            │
                    │ (Fusion multi-sources)       │
                    └──────────────────────────────┘
```

#### B.1 PhaseObserver (Détection Phases Marché)

```
┌─────────────────────────────────────────────────────────────────────────┐
│ PhaseObserver.analyze()                                                 │
│ Fichier: phase_observer/orchestrator.py                                │
├─────────────────────────────────────────────────────────────────────────┤
│ INPUT:                                                                   │
│   - df_m5: DataFrame OHLC M5 (240 bougies)                              │
│   - df_m15: DataFrame OHLC M15 (240 bougies)                            │
│   - asset: "EURUSD" / "GBPUSD" / "XAUUSD"                               │
│                                                                          │
│ PROCESSUS:                                                               │
│                                                                          │
│ 1. FeaturesExtractor.extract(df_m5, df_m15)                             │
│    phase_observer/features.py::FeaturesExtractor                        │
│    ┌───────────────────────────────────────────────────────────┐       │
│    │ Extraction Features Techniques:                            │       │
│    ├───────────────────────────────────────────────────────────┤       │
│    │ • Chandeliers:                                             │       │
│    │   - body_size (High-Low)                                   │       │
│    │   - upper_wick, lower_wick                                 │       │
│    │   - candle_range (absolut)                                 │       │
│    │                                                             │       │
│    │ • Volatilité:                                              │       │
│    │   - ATR (Average True Range, 14 périodes)                  │       │
│    │   - Rolling Std Dev (20 périodes)                          │       │
│    │                                                             │       │
│    │ • Momentum:                                                │       │
│    │   - RSI proxy (oscillateur maison)                         │       │
│    │   - EMA crossover (9/21)                                   │       │
│    │                                                             │       │
│    │ • Volume Profile:                                          │       │
│    │   - Volume par niveau de prix                              │       │
│    │   - POC (Point of Control) - prix max volume               │       │
│    │   - Value Area (70% volume)                                │       │
│    │                                                             │       │
│    │ • Footprint Snapshots:                                     │       │
│    │   - Delta cumulé (buy - sell volume)                       │       │
│    │   - Imbalance zones                                        │       │
│    └───────────────────────────────────────────────────────────┘       │
│                                                                          │
│ 2. Exécution des 8 Détecteurs                                           │
│    phase_observer/detectors.py::Detectors                               │
│    ┌───────────────────────────────────────────────────────────┐       │
│    │ Détecteur 1: Swing Points                                 │       │
│    │   - _get_swing_points()                                    │       │
│    │   - Détection extrema locaux (HH, LL, LH, HL)              │       │
│    │   - Lookback: 10-20 bougies                                │       │
│    │   - Output: {type: "HH"/"LL", price, index, confidence}    │       │
│    ├───────────────────────────────────────────────────────────┤       │
│    │ Détecteur 2: Trend Detection                              │       │
│    │   - _get_trend()                                           │       │
│    │   - EMA 20/50 crossover                                    │       │
│    │   - Swing points alignment                                 │       │
│    │   - Output: {direction: "UPTREND"/"DOWNTREND"/"RANGE"}     │       │
│    ├───────────────────────────────────────────────────────────┤       │
│    │ Détecteur 3: Order Blocks                                 │       │
│    │   - Détection blocs liquidité (OB)                         │       │
│    │   - Bougie forte + mouvement impulsif suivant              │       │
│    │   - Output: {type: "bullish_OB"/"bearish_OB", zone: []}    │       │
│    ├───────────────────────────────────────────────────────────┤       │
│    │ Détecteur 4: Fair Value Gaps (FVG)                        │       │
│    │   - Détection vides prix (3 bougies consécutives)          │       │
│    │   - Gap = High[i-1] < Low[i+1] (bullish)                   │       │
│    │   - Output: {type: "FVG", gap_size, zone: [min, max]}      │       │
│    ├───────────────────────────────────────────────────────────┤       │
│    │ Détecteur 5: Absorption                                   │       │
│    │   - Volume massif + mouvement prix faible                  │       │
│    │   - Ratio: volume > 2x moyenne ET range < ATR              │       │
│    │   - Output: {absorption_flag: true, direction: "BUY/SELL"} │       │
│    ├───────────────────────────────────────────────────────────┤       │
│    │ Détecteur 6: Stacking (Accumulation)                      │       │
│    │   - Delta Ratio >= 70% (dominance acheteurs/vendeurs)      │       │
│    │   - Sur 3+ bougies consécutives                            │       │
│    │   - Output: {stacking: true, direction, strength}          │       │
│    ├───────────────────────────────────────────────────────────┤       │
│    │ Détecteur 7: Imbalance                                    │       │
│    │   - Buy/Sell ratio >= 70/30                                │       │
│    │   - Sur niveau de prix spécifique                          │       │
│    │   - Output: {imbalance: true, ratio, level}                │       │
│    ├───────────────────────────────────────────────────────────┤       │
│    │ Détecteur 8: Liquidity Sweeps                             │       │
│    │   - Cassure HH/LL (Equal Highs/Lows)                       │       │
│    │   - Rejet rapide (fake breakout)                           │       │
│    │   - Output: {sweep: true, type: "EQH/EQL", zone}           │       │
│    └───────────────────────────────────────────────────────────┘       │
│                                                                          │
│ 3. Fusion des Détections                                                │
│    - Agrégation résultats 8 détecteurs                                  │
│    - Calcul score confiance global                                      │
│    - Détermination phase finale:                                        │
│      ├─ TRENDING_BULLISH (trend + swing + momentum alignés)             │
│      ├─ TRENDING_BEARISH                                                │
│      ├─ RANGE_ACCUMULATION (absorption + stacking)                      │
│      ├─ RANGE_DISTRIBUTION                                              │
│      ├─ LIQUIDITY_HUNT (sweeps + FVG)                                   │
│      └─ UNCERTAIN (signaux contradictoires)                             │
│                                                                          │
│ 4. PhaseMemoryManager.update()                                          │
│    - Stockage phase détectée dans historique                            │
│    - Tracking transitions de phases                                     │
│    - Durée phases (combien de minutes dans phase actuelle)              │
│                                                                          │
│ OUTPUT:                                                                  │
│   PhaseSignal {                                                         │
│     direction: "BUY" / "SELL" / "NEUTRAL",                              │
│     phase: "TRENDING_BULLISH" / "RANGE_ACCUMULATION" / ...,             │
│     confidence: 0.0-1.0,                                                │
│     summary: {                                                          │
│       swing_points: [...],                                              │
│       order_blocks: [...],                                              │
│       fvg_zones: [...],                                                 │
│       absorption_detected: bool,                                        │
│       stacking_detected: bool,                                          │
│       liquidity_sweeps: [...]                                           │
│     }                                                                   │
│   }                                                                     │
└─────────────────────────────────────────────────────────────────────────┘
```

#### B.2 OrderFlow V6 Analysis

```
┌─────────────────────────────────────────────────────────────────────────┐
│ OrderFlow V6 Analysis                                                   │
│ Fichier: phase_observer/detect_orderflow_v6/orderflow_v6.py            │
├─────────────────────────────────────────────────────────────────────────┤
│ INPUT:                                                                   │
│   - df_m1: DataFrame OHLC M1 (120 bougies)                              │
│   - ticks_data: Ticks bruts sur minute actuelle (optionnel)             │
│   - footprint_data: Résultat FootprintM1 (optionnel, enrichissement)    │
│   - asset: "XAUUSD" / "EURUSD" / "GBPUSD"                               │
│                                                                          │
│ PROCESSUS:                                                               │
│                                                                          │
│ 1. DataPreparator.prepare(df_m1)                                        │
│    detect_orderflow_v6/data_preparator.py                               │
│    ┌───────────────────────────────────────────────────────────┐       │
│    │ • Validation schéma (colonnes OHLCV obligatoires)          │       │
│    │ • Détection NaN/Inf                                        │       │
│    │ • Nettoyage outliers (Z-score > 3)                         │       │
│    │ • Interpolation données manquantes (linéaire)              │       │
│    │ • Rescue level: LOW/MEDIUM/HIGH selon qualité              │       │
│    └───────────────────────────────────────────────────────────┘       │
│                                                                          │
│ 2. VolumeAnalyzer.analyze(df_clean)                                     │
│    detect_orderflow_v6/volume_analyzer.py                               │
│    ┌───────────────────────────────────────────────────────────┐       │
│    │ Métriques Volume Calculées:                                │       │
│    ├───────────────────────────────────────────────────────────┤       │
│    │ • CVD (Cumulative Volume Delta):                           │       │
│    │   - Delta = Volume_buy - Volume_sell (par bougie)          │       │
│    │   - CVD = Somme cumulée delta                              │       │
│    │   - CVD_slope = Pente régression linéaire CVD (momentum)   │       │
│    │                                                             │       │
│    │ • Imbalance:                                               │       │
│    │   - Ratio = Buy_volume / (Buy + Sell)                      │       │
│    │   - Imbalance_score = abs(Ratio - 0.5) * 2 (0-1)           │       │
│    │   - Mean_imbalance = Moyenne sur 20 bougies                │       │
│    │                                                             │       │
│    │ • Buy/Sell Ratio:                                          │       │
│    │   - Buy_pct = Buy_volume / Total_volume                    │       │
│    │   - Sell_pct = 1 - Buy_pct                                 │       │
│    │                                                             │       │
│    │ • Volume Total:                                            │       │
│    │   - Total_volume (somme toutes bougies)                    │       │
│    │   - Volume_MA (moyenne mobile 20)                          │       │
│    │   - Volume_spike = Volume > 2x Volume_MA                   │       │
│    └───────────────────────────────────────────────────────────┘       │
│                                                                          │
│ 3. InstitutionalMetrics.compute_volume_profile(df_clean)                │
│    detect_orderflow_v6/institutional_metrics.py                         │
│    ┌───────────────────────────────────────────────────────────┐       │
│    │ Volume Profile Complet:                                    │       │
│    ├───────────────────────────────────────────────────────────┤       │
│    │ • VPOC (Volume Point of Control):                          │       │
│    │   - Niveau de prix avec volume maximum                     │       │
│    │   - Représente "fair value" institutionnelle               │       │
│    │                                                             │       │
│    │ • Value Area (VA):                                         │       │
│    │   - Zone contenant 70% du volume total                     │       │
│    │   - VA_High, VA_Low                                        │       │
│    │                                                             │       │
│    │ • HVN (High Volume Nodes):                                 │       │
│    │   - Niveaux volume > 1.5x moyenne                          │       │
│    │   - Support/Résistances institutionnels                    │       │
│    │                                                             │       │
│    │ • LVN (Low Volume Nodes):                                  │       │
│    │   - Niveaux volume < 0.5x moyenne                          │       │
│    │   - Zones de passage rapide (gaps potentiels)              │       │
│    └───────────────────────────────────────────────────────────┘       │
│                                                                          │
│ 4. PatternDetector.detect_patterns(volume_metrics)                      │
│    detect_orderflow_v6/pattern_detector.py                              │
│    ┌───────────────────────────────────────────────────────────┐       │
│    │ Patterns Détectés:                                         │       │
│    ├───────────────────────────────────────────────────────────┤       │
│    │ • Volume Breakout:                                         │       │
│    │   - Volume spike + mouvement prix > ATR                    │       │
│    │   - Direction aligned avec CVD_slope                       │       │
│    │                                                             │       │
│    │ • Impulse Pattern:                                         │       │
│    │   - 3+ bougies consécutives dans même direction            │       │
│    │   - Volume croissant                                       │       │
│    │   - Delta aligné (même signe)                              │       │
│    │                                                             │       │
│    │ • Absorption:                                              │       │
│    │   - Volume massif + range faible                           │       │
│    │   - Ratio: Volume > 2x MA AND Range < 0.5x ATR             │       │
│    │                                                             │       │
│    │ • Stacking:                                                │       │
│    │   - Delta Ratio >= 70% (3+ bougies)                        │       │
│    │   - Accumulation progressive                               │       │
│    └───────────────────────────────────────────────────────────┘       │
│                                                                          │
│ 5. DivergenceDetector.detect(price, cvd)                                │
│    detect_orderflow_v6/divergence_detector.py                           │
│    ┌───────────────────────────────────────────────────────────┐       │
│    │ • Regular Divergence:                                      │       │
│    │   - Prix: HH, CVD: LH → Divergence baissière              │       │
│    │   - Prix: LL, CVD: HL → Divergence haussière              │       │
│    │                                                             │       │
│    │ • Hidden Divergence:                                       │       │
│    │   - Prix: LH, CVD: HH → Continuation haussière            │       │
│    │   - Prix: HL, CVD: LL → Continuation baissière            │       │
│    └───────────────────────────────────────────────────────────┘       │
│                                                                          │
│ 6. ScoringEngine.calculate_score(all_metrics)                           │
│    detect_orderflow_v6/scoring_engine.py                                │
│    ┌───────────────────────────────────────────────────────────┐       │
│    │ Score Institutionnel (0-100):                              │       │
│    ├───────────────────────────────────────────────────────────┤       │
│    │ Formule:                                                   │       │
│    │   Score = (0.30 × Delta_score) +                           │       │
│    │           (0.25 × Imbalance_score) +                       │       │
│    │           (0.20 × Volume_profile_score) +                  │       │
│    │           (0.15 × Pattern_score) +                         │       │
│    │           (0.10 × Divergence_score)                        │       │
│    │                                                             │       │
│    │ Ajustements:                                               │       │
│    │   - Bonus +10 si VPOC aligné avec direction                │       │
│    │   - Bonus +5 si HVN comme support/résistance               │       │
│    │   - Malus -15 si rescue_level = HIGH (données suspectes)   │       │
│    │                                                             │       │
│    │ Status:                                                    │       │
│    │   - VALID: score >= 60                                     │       │
│    │   - WEAK: 40 <= score < 60                                 │       │
│    │   - SUSPECT: score < 40                                    │       │
│    └───────────────────────────────────────────────────────────┘       │
│                                                                          │
│ 7. ResultBuilder.build_result(all_components)                           │
│    detect_orderflow_v6/result_builder.py                                │
│    - Assemblage résultat final standardisé                              │
│    - Enrichissement avec footprint_data si fourni                       │
│                                                                          │
│ OUTPUT:                                                                  │
│   OrderFlowResult {                                                     │
│     score: 0-100,                                                       │
│     status: "VALID" / "WEAK" / "SUSPECT",                               │
│     bias: "BUY" / "SELL" / "NEUTRAL",                                   │
│     summary: {                                                          │
│       delta_total: float,                                               │
│       cvd_slope: float,                                                 │
│       imbalance: float,                                                 │
│       buy_ratio: float,                                                 │
│       volume_total: float,                                              │
│       vpoc_price: float,                                                │
│       va_high: float, va_low: float,                                    │
│       hvn_levels: [prices],                                             │
│       lvn_levels: [prices]                                              │
│     },                                                                  │
│     patterns: [                                                         │
│       {type: "volume_breakout", confidence: 0.85, ...},                 │
│       {type: "impulse", direction: "BUY", ...}                          │
│     ],                                                                  │
│     divergences: [                                                      │
│       {type: "regular_bearish", strength: 0.7, ...}                     │
│     ],                                                                  │
│     rescue_level: "LOW" / "MEDIUM" / "HIGH"                             │
│   }                                                                     │
└─────────────────────────────────────────────────────────────────────────┘
```

#### B.3 Footprint M1 Analysis

```
┌─────────────────────────────────────────────────────────────────────────┐
│ Footprint M1 Analysis                                                   │
│ Fichier: phase_observer/footprint_analyzer.py                          │
├─────────────────────────────────────────────────────────────────────────┤
│ INPUT:                                                                   │
│   - ticks_data: Ticks bruts MT5 sur minute actuelle                     │
│   - asset: "XAUUSD" / "EURUSD" / "GBPUSD"                               │
│                                                                          │
│ PROCESSUS:                                                               │
│                                                                          │
│ 1. DataValidator.validate(ticks_data)                                   │
│    ┌───────────────────────────────────────────────────────────┐       │
│    │ • Vérification colonnes: bid, ask, volume, flags, time     │       │
│    │ • Détection données corrompues (NaN, négatifs)             │       │
│    │ • Calcul tick_count, coverage_seconds                      │       │
│    │ • Status qualité:                                          │       │
│    │   - VALID: tick_count >= 50, coverage >= 30s               │       │
│    │   - SUSPECT: 20 <= tick_count < 50 ou coverage < 30s       │       │
│    │   - INVALID: tick_count < 20                               │       │
│    └───────────────────────────────────────────────────────────┘       │
│                                                                          │
│ 2. Normalisation Données Feeds                                          │
│    ┌───────────────────────────────────────────────────────────┐       │
│    │ • Uniformisation formats (MT5 vs autres brokers)           │       │
│    │ • Conversion timestamps (UTC)                              │       │
│    │ • Détection trade direction (buy/sell) via flags           │       │
│    │   - Flag & 2 = BUY_FLAG (tick ask)                         │       │
│    │   - Sinon = SELL_FLAG (tick bid)                           │       │
│    └───────────────────────────────────────────────────────────┘       │
│                                                                          │
│ 3. Calcul Métriques Footprint                                           │
│    ┌───────────────────────────────────────────────────────────┐       │
│    │ Métriques Calculées:                                       │       │
│    ├───────────────────────────────────────────────────────────┤       │
│    │ • Buy Volume / Sell Volume:                                │       │
│    │   - Somme volume ticks BUY vs SELL                         │       │
│    │                                                             │       │
│    │ • Delta Total:                                             │       │
│    │   - Delta = Buy_volume - Sell_volume                       │       │
│    │                                                             │       │
│    │ • POC (Point of Control):                                  │       │
│    │   - Niveau prix avec volume maximum                        │       │
│    │   - Calculé via histogram prix/volume                      │       │
│    │                                                             │       │
│    │ • Tick Rate:                                               │       │
│    │   - Ticks/seconde = tick_count / coverage_seconds          │       │
│    │   - Mesure activité marché                                 │       │
│    │                                                             │       │
│    │ • Volume Profile:                                          │       │
│    │   - Distribution volume par niveau de prix                 │       │
│    │   - Bins: Discrétisation prix en intervals                 │       │
│    └───────────────────────────────────────────────────────────┘       │
│                                                                          │
│ 4. Détection Triggers (PARTIELLEMENT SUPPRIMÉ)                          │
│    Note: Triggers historiques (absorption, stacking, imbalance)          │
│    largement remplacés par VWAP. Code legacy conservé pour debug.       │
│    ┌───────────────────────────────────────────────────────────┐       │
│    │ [LEGACY] Absorption:                                       │       │
│    │   - Volume > 2x moyenne ET range < ATR                     │       │
│    │                                                             │       │
│    │ [LEGACY] Stacking:                                         │       │
│    │   - Delta Ratio >= 70%                                     │       │
│    │                                                             │       │
│    │ [LEGACY] Imbalance:                                        │       │
│    │   - Buy/Sell >= 70/30                                      │       │
│    └───────────────────────────────────────────────────────────┘       │
│                                                                          │
│ 5. Scoring                                                               │
│    ┌───────────────────────────────────────────────────────────┐       │
│    │ Score (0-100):                                             │       │
│    │   Base = 50 (si VALID)                                     │       │
│    │   + Delta bonus (±10 selon |delta|)                        │       │
│    │   + Tick rate bonus (jusqu'à +10 si > 50 ticks/s)          │       │
│    │   - Malus qualité (-20 si SUSPECT, -50 si INVALID)         │       │
│    └───────────────────────────────────────────────────────────┘       │
│                                                                          │
│ OUTPUT:                                                                  │
│   FootprintResult {                                                     │
│     status: "VALID" / "SUSPECT" / "INVALID",                            │
│     score: 0-100,                                                       │
│     summary: {                                                          │
│       buy_volume: float,                                                │
│       sell_volume: float,                                               │
│       delta_total: float,                                               │
│       poc: float,                                                       │
│       tick_count: int,                                                  │
│       coverage_s: float,                                                │
│       tick_rate: float                                                  │
│     },                                                                  │
│     absorption_flag: bool,  // DEPRECATED                               │
│     stacking_flag: bool,    // DEPRECATED                               │
│     imbalance_flag: bool    // DEPRECATED                               │
│   }                                                                     │
└─────────────────────────────────────────────────────────────────────────┘
```

#### B.4 VWAP Analysis

```
┌─────────────────────────────────────────────────────────────────────────┐
│ VWAP Institutionnel Analysis                                           │
│ Fichier: phase_observer/vwap/analyzer.py                               │
├─────────────────────────────────────────────────────────────────────────┤
│ INPUT:                                                                   │
│   - df_m1: DataFrame OHLC M1 (120 bougies)                              │
│   - asset: "XAUUSD" / "EURUSD" / "GBPUSD"                               │
│   - session_config: Configuration session trading (optionnel)           │
│                                                                          │
│ PROCESSUS:                                                               │
│                                                                          │
│ 1. Validators.validate_data(df_m1)                                      │
│    vwap/validators.py                                                   │
│    ┌───────────────────────────────────────────────────────────┐       │
│    │ • Vérification colonnes OHLCV + timestamp                  │       │
│    │ • Détection NaN/Inf                                        │       │
│    │ • Validation chronologique (pas de trous > 5 min)          │       │
│    │ • Qualité status: VALID / SUSPECT / INVALID                │       │
│    └───────────────────────────────────────────────────────────┘       │
│                                                                          │
│ 2. VWAPCalculator.calculate_standard_vwap(df_m1)                        │
│    vwap/core.py                                                         │
│    ┌───────────────────────────────────────────────────────────┐       │
│    │ VWAP Standard (depuis minuit session):                     │       │
│    ├───────────────────────────────────────────────────────────┤       │
│    │ Formule:                                                   │       │
│    │   Typical Price = (High + Low + Close) / 3                 │       │
│    │   TP×Volume = Typical_Price × Volume                       │       │
│    │   Cumul_TP_Vol = Somme cumulée TP×Volume                   │       │
│    │   Cumul_Volume = Somme cumulée Volume                      │       │
│    │   VWAP = Cumul_TP_Vol / Cumul_Volume                       │       │
│    │                                                             │       │
│    │ Reset:                                                     │       │
│    │   - VWAP reset à minuit UTC (nouveau jour)                 │       │
│    │   - Optionnel: Reset par session (London, NY, Asia)        │       │
│    └───────────────────────────────────────────────────────────┘       │
│                                                                          │
│ 3. DerivativesCalculator.calculate_derivatives(vwap, price)             │
│    vwap/derivatives.py                                                  │
│    ┌───────────────────────────────────────────────────────────┐       │
│    │ Dérivées VWAP:                                             │       │
│    ├───────────────────────────────────────────────────────────┤       │
│    │ • VWAP Slope (Momentum):                                   │       │
│    │   - Pente régression linéaire VWAP (10 bougies)            │       │
│    │   - Positive → Tendance haussière institutionnelle         │       │
│    │   - Négative → Tendance baissière                          │       │
│    │                                                             │       │
│    │ • VWAP Velocity (Accélération):                            │       │
│    │   - Dérivée seconde (slope de slope)                       │       │
│    │   - Détection accélération/décélération mouvement          │       │
│    │                                                             │       │
│    │ • Distance VWAP:                                           │       │
│    │   - Distance_pips = (Price - VWAP) / point_size            │       │
│    │   - Distance_pct = (Price - VWAP) / VWAP × 100             │       │
│    │                                                             │       │
│    │ • Standard Deviation Bands:                                │       │
│    │   - Upper_band = VWAP + (k × StdDev)                       │       │
│    │   - Lower_band = VWAP - (k × StdDev)                       │       │
│    │   - k = 1.0, 1.5, 2.0 (configs)                            │       │
│    └───────────────────────────────────────────────────────────┘       │
│                                                                          │
│ 4. SignalGenerator.generate_signals(vwap, price, derivatives)           │
│    vwap/signals.py                                                      │
│    ┌───────────────────────────────────────────────────────────┐       │
│    │ Signaux VWAP:                                              │       │
│    ├───────────────────────────────────────────────────────────┤       │
│    │ • Crossover Signal:                                        │       │
│    │   - Prix cross VWAP haut → bas = Signal SELL               │       │
│    │   - Prix cross VWAP bas → haut = Signal BUY                │       │
│    │                                                             │       │
│    │ • Proximity Signal:                                        │       │
│    │   - Prix near VWAP (distance < 10 pips XAUUSD)             │       │
│    │   - Mean reversion opportunité                             │       │
│    │                                                             │       │
│    │ • Bounce Signal:                                           │       │
│    │   - Prix touche VWAP + rejection (wick)                    │       │
│    │   - VWAP = support (uptrend) ou résistance (downtrend)     │       │
│    │                                                             │       │
│    │ • Band Signals:                                            │       │
│    │   - Prix > Upper_band = Overbought                         │       │
│    │   - Prix < Lower_band = Oversold                           │       │
│    └───────────────────────────────────────────────────────────┘       │
│                                                                          │
│ 5. RegimeMapper.map_regime(vwap, price, volume, slope)                  │
│    vwap/regime_mapper.py                                                │
│    ┌───────────────────────────────────────────────────────────┐       │
│    │ Régimes VWAP (4 états):                                    │       │
│    ├───────────────────────────────────────────────────────────┤       │
│    │ • TRENDING:                                                │       │
│    │   - Slope significatif (abs(slope) > seuil)                │       │
│    │   - Prix maintenu au-dessus/en-dessous VWAP               │       │
│    │   - Volume soutenu                                         │       │
│    │   → Poids VWAP: 50% (prioritaire)                          │       │
│    │                                                             │       │
│    │ • BALANCED:                                                │       │
│    │   - Prix oscille autour VWAP (mean reversion)              │       │
│    │   - Slope faible                                           │       │
│    │   - Volume normal                                          │       │
│    │   → Poids VWAP: 30% (référence neutre)                     │       │
│    │                                                             │       │
│    │ • ACCUMULATION:                                            │       │
│    │   - Prix consolidation près VWAP                           │       │
│    │   - Volume faible                                          │       │
│    │   - Range serré                                            │       │
│    │   → Poids VWAP: 25% (support/résistance)                   │       │
│    │                                                             │       │
│    │ • TRANSITIONAL:                                            │       │
│    │   - Slope chaotique (changements rapides)                  │       │
│    │   - Volatilité élevée                                      │       │
│    │   - Compression/expansion rapide                           │       │
│    │   → Poids VWAP: 20% (peu fiable)                           │       │
│    └───────────────────────────────────────────────────────────┘       │
│                                                                          │
│ 6. MetricsCollector.collect_metrics(all_data)                           │
│    vwap/metrics.py                                                      │
│    ┌───────────────────────────────────────────────────────────┐       │
│    │ KPIs:                                                      │       │
│    │ • Distance moyenne VWAP (sur fenêtre)                      │       │
│    │ • Deviation % (écart type)                                 │       │
│    │ • Touch count (combien fois prix touche VWAP)              │       │
│    │ • Bounce rate (% touches avec rejection)                   │       │
│    └───────────────────────────────────────────────────────────┘       │
│                                                                          │
│ 7. CacheManager.cache_result(asset, result)                             │
│    vwap/cache.py                                                        │
│    ┌───────────────────────────────────────────────────────────┐       │
│    │ • Cache session (reset minuit UTC)                         │       │
│    │ • Cache intraday (TTL 60 secondes)                         │       │
│    │ • Évite recalcul VWAP à chaque cycle                       │       │
│    └───────────────────────────────────────────────────────────┘       │
│                                                                          │
│ OUTPUT:                                                                  │
│   VWAPAnalysisResult {                                                  │
│     score: 0.0-1.0,  // Normalisé (vs 0-100 OrderFlow)                  │
│     status: "VALID" / "SUSPECT" / "INVALID",                            │
│     bias: "BULLISH" / "BEARISH" / "NEUTRAL",                            │
│     vwap_value: float,                                                  │
│     distance_pips: float,                                               │
│     distance_pct: float,                                                │
│     slope: float,                                                       │
│     velocity: float,                                                    │
│     zone: "NEUTRAL" / "STRONG" / "EXTREME",                             │
│     regime: "TRENDING" / "BALANCED" / "ACCUMULATION" / "TRANSITIONAL",  │
│     signals: [                                                          │
│       {type: "crossover", direction: "BUY", confidence: 0.8},           │
│       {type: "proximity", distance: 8.5, note: "near_vwap"}             │
│     ],                                                                  │
│     summary: {                                                          │
│       upper_band: float,                                                │
│       lower_band: float,                                                │
│       touch_count: int,                                                 │
│       bounce_rate: float                                                │
│     }                                                                   │
│   }                                                                     │
└─────────────────────────────────────────────────────────────────────────┘
```

#### B.5 FusionManager (Fusion Multi-Sources)

```
┌─────────────────────────────────────────────────────────────────────────┐
│ FusionManager - Fusion Multi-Sources                                    │
│ Fichier: phase_observer/fusion_manager.py::fuse()                      │
├─────────────────────────────────────────────────────────────────────────┤
│ INPUT:                                                                   │
│   - orderflow: OrderFlowResult (B.2)                                    │
│   - footprint: FootprintResult (B.3)                                    │
│   - vwap: VWAPAnalysisResult (B.4)                                      │
│   - strategy_config: Configuration stratégie (seuils)                   │
│   - context: {regime, volatility, session, asset, ...}                  │
│                                                                          │
│ PROCESSUS:                                                               │
│                                                                          │
│ 1. Validation Inputs                                                    │
│    _validate_inputs(orderflow, footprint, vwap)                         │
│    ┌───────────────────────────────────────────────────────────┐       │
│    │ • Vérification schémas (score, status présents)            │       │
│    │ • Détection NaN / types invalides                          │       │
│    │ • Calcul quality_score (0-1):                              │       │
│    │   - Pénalité -0.05 par warning                             │       │
│    │   - Pénalité -0.50 si champs manquants                     │       │
│    │ • missing: [champs absents]                                │       │
│    │ • warnings: [anomalies non-bloquantes]                     │       │
│    └───────────────────────────────────────────────────────────┘       │
│                                                                          │
│ 2. Normalisation Composants                                             │
│    ┌───────────────────────────────────────────────────────────┐       │
│    │ _normalize_orderflow(orderflow)                            │       │
│    │   - Normalise score 0-100 → 0-1                            │       │
│    │   - Direction: BUY/SELL/NEUTRAL → +1/-1/0                  │       │
│    │   - Extraction delta_total, poc, absorption                │       │
│    │                                                             │       │
│    │ _normalize_footprint(footprint)                            │       │
│    │   - Score 0-100 → 0-1 (ou heuristique si absent)           │       │
│    │   - Direction delta → +1/-1/0                              │       │
│    │   - Extraction buy/sell volumes, tick_rate                 │       │
│    │                                                             │       │
│    │ _normalize_vwap(vwap)                                      │       │
│    │   - Score déjà 0-1 (pas de conversion)                     │       │
│    │   - Bias BULLISH/BEARISH/NEUTRAL → +1/-1/0                 │       │
│    │   - Extraction slope, distance_pips, regime                │       │
│    └───────────────────────────────────────────────────────────┘       │
│                                                                          │
│ 3. Analyse Cohérence                                                    │
│    _analyze_coherence(n_of, n_fp, n_vw)                                 │
│    ┌───────────────────────────────────────────────────────────┐       │
│    │ Votes Pondérés:                                            │       │
│    │   - OrderFlow:  direction × score                          │       │
│    │   - Footprint:  direction × score                          │       │
│    │   - VWAP:       direction × score                          │       │
│    │                                                             │       │
│    │ Majorité:                                                  │       │
│    │   pos_weight = Somme(votes BUY)                            │       │
│    │   neg_weight = Somme(votes SELL)                           │       │
│    │   majority = +1 si pos > neg, -1 si neg > pos, 0 si égal   │       │
│    │                                                             │       │
│    │ Agreement Score:                                           │       │
│    │   agreement = max(pos, neg) / total_weight                 │       │
│    │   → 0.0-1.0 (1.0 = consensus parfait)                      │       │
│    │                                                             │       │
│    │ Matrice Cohérence:                                         │       │
│    │   - of_vs_fp: "aligned" / "conflict" / "neutral"           │       │
│    │   - of_vs_vwap: "aligned" / "conflict" / "neutral"         │       │
│    │   - fp_vs_vwap: "aligned" / "conflict" / "neutral"         │       │
│    │                                                             │       │
│    │ Lead/Lag (timestamps):                                     │       │
│    │   - orderflow_age_s, footprint_age_s, vwap_age_s           │       │
│    │   - Détecte quel signal est le plus récent                 │       │
│    └───────────────────────────────────────────────────────────┘       │
│                                                                          │
│ 4. Règles Métier                                                        │
│    _apply_business_rules(n_of, n_fp, n_vw, coherence)                   │
│    ┌───────────────────────────────────────────────────────────┐       │
│    │ Règles Actives:                                            │       │
│    ├───────────────────────────────────────────────────────────┤       │
│    │ • WEAK FOOTPRINT:                                          │       │
│    │   - Si footprint.score < 0.60 → Note warning               │       │
│    │   - Pas de veto (contrairement à ancien système)           │       │
│    │                                                             │       │
│    │ • TRIPLE CONFIRMATION BONUS:                               │       │
│    │   - Si of_vs_fp = aligned ET                               │       │
│    │        of_vs_vwap = aligned ET                             │       │
│    │        fp_vs_vwap = aligned                                │       │
│    │   → Bonus confiance +0.05                                  │       │
│    └───────────────────────────────────────────────────────────┘       │
│                                                                          │
│ 5. Poids Adaptatifs                                                     │
│    _adaptive_weights(regime, volatility, session, vwap_regime)          │
│    ┌───────────────────────────────────────────────────────────┐       │
│    │ PRIORITÉ 1: VWAP Regime (NOUVEAU - 06 DEC 2025)           │       │
│    ├───────────────────────────────────────────────────────────┤       │
│    │ • TRENDING:                                                │       │
│    │   - VWAP: 50%, OrderFlow: 30%, Footprint: 20%             │       │
│    │   - Logique: Tendance institutionnelle, VWAP principal     │       │
│    │                                                             │       │
│    │ • BALANCED:                                                │       │
│    │   - VWAP: 30%, OrderFlow: 35%, Footprint: 35%             │       │
│    │   - Logique: Équilibre, VWAP référence neutre              │       │
│    │                                                             │       │
│    │ • ACCUMULATION:                                            │       │
│    │   - VWAP: 25%, OrderFlow: 35%, Footprint: 40%             │       │
│    │   - Logique: Micro-structure (FP) dominante               │       │
│    │                                                             │       │
│    │ • TRANSITIONAL:                                            │       │
│    │   - VWAP: 20%, OrderFlow: 40%, Footprint: 40%             │       │
│    │   - Logique: Chaos, VWAP peu fiable, focus OF+FP          │       │
│    ├───────────────────────────────────────────────────────────┤       │
│    │ PRIORITÉ 2: Ajustements Legacy (si pas vwap_regime)       │       │
│    ├───────────────────────────────────────────────────────────┤       │
│    │ Fallback Poids:                                            │       │
│    │   - OrderFlow: 30%, Footprint: 35%, VWAP: 35%             │       │
│    │                                                             │       │
│    │ Ajustements Volatilité:                                    │       │
│    │   - Volatilité HIGH: OrderFlow +10%, VWAP -10%            │       │
│    │                                                             │       │
│    │ Ajustements Regime (PhaseObserver):                        │       │
│    │   - TRENDING: OrderFlow +5%, VWAP +3%, FP -8%             │       │
│    │   - RANGE: Footprint +10%, OF -5%, VWAP -5%               │       │
│    │                                                             │       │
│    │ Ajustements Session:                                       │       │
│    │   - London/NY: OrderFlow +3%, VWAP +2%, FP -5%            │       │
│    │   - Asia: Footprint +5%, OrderFlow -3%, VWAP -2%          │       │
│    │                                                             │       │
│    │ Normalisation finale:                                      │       │
│    │   total = w_of + w_fp + w_vw                               │       │
│    │   w_of, w_fp, w_vw = w / total  (somme = 1.0)             │       │
│    └───────────────────────────────────────────────────────────┘       │
│                                                                          │
│ 6. Calcul Confiance Fusionnée                                           │
│    _calculate_fused_confidence(n_of, n_fp, n_vw, coherence, weights)    │
│    ┌───────────────────────────────────────────────────────────┐       │
│    │ Formule Pondérée:                                          │       │
│    │   weighted_score = (of_score × w_of) +                     │       │
│    │                    (fp_score × w_fp) +                     │       │
│    │                    (vw_score × w_vw)                       │       │
│    │                                                             │       │
│    │ Bonus Cohérence:                                           │       │
│    │   - Si alignments >= 3: +0.05                              │       │
│    │   - Timing bonus: +0.0 à +0.03 selon fraîcheur signaux     │       │
│    │                                                             │       │
│    │ Malus Conflits:                                            │       │
│    │   - Si conflicts >= 2: ×0.85 (-15%)                        │       │
│    │   - Si conflicts == 1: ×0.92 (-8%)                         │       │
│    │                                                             │       │
│    │ Normalisation:                                             │       │
│    │   final_score = max(0.0, min(0.99, weighted_score))        │       │
│    │                                                             │       │
│    │ Contribution VWAP (tracking):                              │       │
│    │   vwap_contribution = vw_score × w_vw                      │       │
│    └───────────────────────────────────────────────────────────┘       │
│                                                                          │
│ 7. Génération Décision Finale                                           │
│    _final_decision(fused_score, n_vw, coherence, thresholds)            │
│    ┌───────────────────────────────────────────────────────────┐       │
│    │ Direction:                                                 │       │
│    │   - Majorité pondérée (coherence["majority"])              │       │
│    │   - Fallback VWAP bias si égalité                          │       │
│    │                                                             │       │
│    │ Anchor Price:                                              │       │
│    │   - VWAP value (support/résistance dynamique)              │       │
│    │                                                             │       │
│    │ Seuils Décision (depuis config):                           │       │
│    │   - high: 0.60 → HIGH_CONVICTION_BUY/SELL                  │       │
│    │   - moderate: 0.55 → MODERATE_BUY/SELL                     │       │
│    │   - cautious: 0.50 → CAUTIOUS_BUY/SELL                     │       │
│    │   - conditional: 0.35 → CONDITIONAL_BUY/SELL               │       │
│    │   - < conditional → HOLD (WAIT_CONFIRMATION)               │       │
│    │                                                             │       │
│    │ Action:                                                    │       │
│    │   - "BUY" / "SELL" / "HOLD"                                │       │
│    └───────────────────────────────────────────────────────────┘       │
│                                                                          │
│ 8. Construction Rationale                                               │
│    _rationale(decision, n_of, n_fp, n_vw, coherence, rules)             │
│    - Texte explicatif décision (logs)                                   │
│    - Format: "{signal_type} car orderflow {dir} | footprint {dir} |     │
│               vwap {bias} | cohérence={agreement} | règles={...}"       │
│                                                                          │
│ OUTPUT:                                                                  │
│   FusionResult {                                                        │
│     ok: bool,  // True si action != HOLD ET score >= seuil min          │
│     action: "BUY" / "SELL" / "HOLD",                                    │
│     signal_type: "HIGH_CONVICTION_BUY" / "MODERATE_SELL" / ...,         │
│     direction: "BUY" / "SELL" / "NEUTRAL",                              │
│     fused_confidence: 0.0-0.99,                                         │
│     vwap_contribution: 0.0-1.0,  // Part VWAP dans score final          │
│     anchor_price: float (VWAP value),                                   │
│     rationale: str,                                                     │
│     components: {                                                       │
│       orderflow: n_of,                                                  │
│       footprint: n_fp,                                                  │
│       vwap: n_vw                                                        │
│     },                                                                  │
│     consensus: {                                                        │
│       maj: "BUY" / "SELL" / "TIE",                                      │
│       agreement: 0.0-1.0,                                               │
│       votes: [("orderflow", "BUY", 0.75), ...]                          │
│     },                                                                  │
│     quality: {                                                          │
│       is_valid: bool,                                                   │
│       quality_score: 0.0-1.0,                                           │
│       missing: [champs manquants],                                      │
│       warnings: [anomalies]                                             │
│     }                                                                   │
│   }                                                                     │
└─────────────────────────────────────────────────────────────────────────┘
```

---

## 5. PIPELINE DE DÉCISION

### 5.1 Détail Étape C: DECISION MAKING

```
┌─────────────────────────────────────────────────────────────────────────┐
│ C. DECISION MAKING                                                      │
│ Fichier: core/decision_pipeline.py::run()                              │
│ Appelé par: run_single_pipeline_cycle()                                │
└─────────────────────────────────────────────────────────────────────────┘
                                   │
                    ┌──────────────┴──────────────┐
                    ▼                             ▼
        ┌───────────────────┐          ┌──────────────────┐
        │ C.1 Per-Asset     │          │ C.2 Strategy     │
        │ Evaluation        │─────────▶│ Selection        │
        └───────────────────┘          └──────────────────┘
                                               │
                    ┌──────────────────────────┴──────────────────────────┐
                    ▼                                                     ▼
        ┌───────────────────────────┐                  ┌──────────────────────────┐
        │ C.3 Scalping Pipeline     │                  │ C.4 Liquidity Strategy   │
        │ (Phase-Free)              │                  │ (Phase-Based)            │
        └───────────────────────────┘                  └──────────────────────────┘
                    │                                                     │
                    └──────────────────────────┬──────────────────────────┘
                                               ▼
                                ┌──────────────────────────────┐
                                │ C.5 Final Decision           │
                                │ (Agrégation + Filtering)     │
                                └──────────────────────────────┘
```

#### C.1 Per-Asset Evaluation Loop

```
┌─────────────────────────────────────────────────────────────────────────┐
│ DecisionPipeline.run()                                                  │
│ Fichier: core/decision_pipeline.py (lignes ~500-800)                   │
├─────────────────────────────────────────────────────────────────────────┤
│ INPUT:                                                                   │
│   - market_context: Dict avec {EURUSD: data, GBPUSD: data, XAUUSD: ...} │
│   - config_manager: Instance ConfigManager                              │
│   - strategy_manager: Instance StrategyManager                          │
│                                                                          │
│ PROCESSUS:                                                               │
│                                                                          │
│ 1. Extraction Liste Actifs Autorisés                                    │
│    ┌───────────────────────────────────────────────────────────┐       │
│    │ base_config = config_manager.get_current_dynamic_config() │       │
│    │ global_allowed = base_config["global_safety"]["global_allowed_symbols"] │
│    │ account_details = config_manager.get_mt5_account_credentials()     │
│    │ account_allowed = account_details["allowed_symbols"]               │
│    │ final_symbols = intersection(global_allowed, account_allowed)      │
│    │ → ["EURUSD", "GBPUSD", "XAUUSD"]  // Typiquement                   │
│    └───────────────────────────────────────────────────────────┘       │
│                                                                          │
│ 2. Boucle For Each Asset                                                │
│    for asset in final_symbols:                                          │
│    ┌───────────────────────────────────────────────────────────┐       │
│    │ 2.1 Chargement Config Asset                               │       │
│    │     config_manager.load_asset_config(asset)                │       │
│    │     → Charge config/assets/{asset}.json                    │       │
│    │     → Merge avec prod_config.json (overrides prioritaires)│       │
│    │                                                             │       │
│    │ 2.2 Extraction Market Data pour Asset                     │       │
│    │     df_m1 = market_context[asset]["m1"]                    │       │
│    │     df_m5 = market_context[asset]["m5"]                    │       │
│    │     df_m15 = market_context[asset]["m15"]                  │       │
│    │     ticks = market_context[asset]["ticks"]                 │       │
│    │     symbol_info = market_context[asset]["symbol_info"]     │       │
│    │                                                             │       │
│    │ 2.3 Appel MarketAnalyzer pour Signaux                     │       │
│    │     signals = market_analyzer.analyze(                     │       │
│    │         df_m1, df_m5, df_m15, ticks, asset                 │       │
│    │     )                                                      │       │
│    │     → Retour FusionResult (voir section B.5)               │       │
│    │                                                             │       │
│    │ 2.4 Sélection Stratégie                                   │       │
│    │     strategy_name = merged_config["strategy_name"]         │       │
│    │     → "scalping" ou "liquidity" (depuis asset.json)        │       │
│    │                                                             │       │
│    │ 2.5 Exécution Stratégie                                   │       │
│    │     IF strategy_name == "scalping":                        │       │
│    │         decision = scalping_pipeline.run(...)              │       │
│    │     ELIF strategy_name == "liquidity":                     │       │
│    │         decision = liquidity_strategy.evaluate_entry(...)  │       │
│    │                                                             │       │
│    │ 2.6 Stockage Décision                                     │       │
│    │     decisions[asset] = decision                            │       │
│    └───────────────────────────────────────────────────────────┘       │
│                                                                          │
│ 3. Filtrage & Priorisation                                              │
│    ┌───────────────────────────────────────────────────────────┐       │
│    │ • Suppression decisions avec action="HOLD"                 │       │
│    │ • Tri par confidence décroissant                           │       │
│    │ • Limite 1 trade par cycle (daily_trade_count check)       │       │
│    │ • Vérification limites globales:                           │       │
│    │   - max_open_positions                                     │       │
│    │   - max_risk_exposure_usd                                  │       │
│    │   - max_daily_trades                                       │       │
│    └───────────────────────────────────────────────────────────┘       │
│                                                                          │
│ OUTPUT:                                                                  │
│   final_decision: {                                                     │
│     asset: "XAUUSD",                                                    │
│     action: "BUY",                                                      │
│     confidence: 0.72,                                                   │
│     signal_type: "HIGH_CONVICTION_BUY",                                 │
│     strategy_name: "scalping",                                          │
│     execution_params: {                                                 │
│       entry_price: 2050.50,  // Estimé                                  │
│       anchor_price: 2048.30,  // VWAP                                   │
│       burst_size: 8,  // Si scalping burst                              │
│       sl_method: "ATR", tp_method: "RR", rr_ratio: 2.5                  │
│     },                                                                  │
│     context_snapshot: {                                                 │
│       cycle: 42,                                                        │
│       timestamp: "2025-12-06T14:32:00Z",                                │
│       market_phase: "TRENDING_BULLISH",  // PhaseObserver               │
│       vwap_regime: "TRENDING",  // VWAP                                 │
│       orderflow_score: 78, footprint_score: 65, vwap_score: 0.82        │
│     }                                                                   │
│   }                                                                     │
│   OU null si aucune décision valide                                     │
└─────────────────────────────────────────────────────────────────────────┘
```

#### C.2 Scalping Pipeline (Phase-Free)

```
┌─────────────────────────────────────────────────────────────────────────┐
│ ScalpingPipeline.run()                                                  │
│ Fichier: strategy/pipeline.py                                          │
├─────────────────────────────────────────────────────────────────────────┤
│ INPUT:                                                                   │
│   - df_m1: DataFrame OHLC M1                                            │
│   - asset: "XAUUSD" / "EURUSD" / "GBPUSD"                               │
│   - signals: FusionResult (depuis MarketAnalyzer)                       │
│   - merged_config: Configuration fusionnée asset + stratégie            │
│                                                                          │
│ PROCESSUS:                                                               │
│                                                                          │
│ 1. Validation Footprint M1                                              │
│    footprint_status = signals["components"]["footprint"]["status"]      │
│    IF footprint_status == "INVALID":                                    │
│        RETURN {} (pas de trade sans footprint valide)                   │
│                                                                          │
│ 2. Détection OrderFlow                                                  │
│    orderflow_score = signals["components"]["orderflow"]["score"]        │
│    orderflow_bias = signals["components"]["orderflow"]["bias"]          │
│                                                                          │
│ 3. Fusion Signaux (déjà fait par MarketAnalyzer)                        │
│    fused_confidence = signals["fused_confidence"]                       │
│    consensus_direction = signals["consensus"]["maj"]                    │
│                                                                          │
│ 4. Appel ScalpingStrategy.evaluate_entry()                              │
│    strategy/scalping.py::ScalpingStrategy                               │
│    ┌───────────────────────────────────────────────────────────┐       │
│    │ INPUT:                                                     │       │
│    │   - asset, df_m1, market_data, signals, merged_config     │       │
│    │                                                             │       │
│    │ PROCESSUS:                                                 │       │
│    │                                                             │       │
│    │ 4.1 Extraction Règles d'Entrée                            │       │
│    │     entry_rules = merged_config["entry_rules"]["scalping"] │       │
│    │     burst_config = entry_rules["burst_scalping"]           │       │
│    │     ┌─────────────────────────────────────────────┐       │       │
│    │     │ • min_confidence: 0.55 (seuil minimum)      │       │       │
│    │     │ • burst_size: 8 (nombre tickets panier)     │       │       │
│    │     │ • burst_enabled: true                       │       │       │
│    │     │ • max_spread_pips: 2.0 (XAUUSD)             │       │       │
│    │     └─────────────────────────────────────────────┘       │       │
│    │                                                             │       │
│    │ 4.2 Vérification Confiance                                │       │
│    │     IF fused_confidence < min_confidence:                  │       │
│    │         RETURN {"action": "HOLD", "rule_name": "MIN_CONF"} │       │
│    │                                                             │       │
│    │ 4.3 Vérification Spread                                   │       │
│    │     current_spread = symbol_info.spread / 10.0  // pips    │       │
│    │     IF current_spread > max_spread_pips:                   │       │
│    │         RETURN {"action": "HOLD", "rule_name": "SPREAD"}   │       │
│    │                                                             │       │
│    │ 4.4 Vérification Direction Consensus                      │       │
│    │     IF consensus_direction == "TIE":                       │       │
│    │         RETURN {"action": "HOLD", "rule_name": "NO_CONSENSUS"} │  │
│    │                                                             │       │
│    │ 4.5 Construction Décision                                 │       │
│    │     action = "BUY" if consensus_direction == "BUY" else "SELL" │  │
│    │     execution_params = {                                   │       │
│    │         "burst_enabled": burst_enabled,                    │       │
│    │         "burst_size": burst_size,                          │       │
│    │         "entry_mode": "MARKET",  // Entrée immédiate       │       │
│    │         "sl_method": "ATR",  // SL dynamique               │       │
│    │         "tp_method": None,  // Pas de TP (trailing only)   │       │
│    │         "trailing_enabled": false,  // Géré par executor   │       │
│    │     }                                                      │       │
│    │                                                             │       │
│    │ OUTPUT:                                                    │       │
│    │   {                                                        │       │
│    │     "action": "BUY" / "SELL" / "HOLD",                     │       │
│    │     "asset": asset,                                        │       │
│    │     "confidence": fused_confidence,                        │       │
│    │     "rule_name": "SCALPING_BURST_ENTRY",                   │       │
│    │     "execution_params": execution_params,                  │       │
│    │     "execution_status": "PENDING"                          │       │
│    │   }                                                        │       │
│    └───────────────────────────────────────────────────────────┘       │
│                                                                          │
│ 5. Pre-gate Arbiter (Monitoring Only)                                   │
│    - Log décision pour audit                                            │
│    - Pas de blocage (contrairement à ancien système)                    │
│                                                                          │
│ OUTPUT:                                                                  │
│   Decision Dict (retourné à DecisionPipeline)                           │
└─────────────────────────────────────────────────────────────────────────┘
```

#### C.3 Liquidity Strategy (Phase-Based)

```
┌─────────────────────────────────────────────────────────────────────────┐
│ LiquidityStrategy.evaluate_entry()                                      │
│ Fichier: strategy/liquidity.py                                         │
├─────────────────────────────────────────────────────────────────────────┤
│ INPUT:                                                                   │
│   - df_m5, df_m15: DataFrames OHLC                                      │
│   - asset: "EURUSD" / "GBPUSD" / "XAUUSD"                               │
│   - market_data: Dict avec symbol_info, account_info                    │
│   - phase_signal: PhaseSignal (depuis PhaseObserver)                    │
│                                                                          │
│ PROCESSUS:                                                               │
│                                                                          │
│ 1. Vérification Phase Requise                                           │
│    IF phase_signal is None:                                             │
│        RETURN {} (stratégie nécessite PhaseObserver)                    │
│                                                                          │
│ 2. Extraction Entry Rules                                               │
│    entry_rules = strategy_config["entry_rules"]["liquidity"]            │
│    ┌───────────────────────────────────────────────────────────┐       │
│    │ • required_phase: ["LIQUIDITY_HUNT", "RANGE_DISTRIBUTION"] │       │
│    │ • min_confidence: 0.60                                     │       │
│    │ • require_ob: true  // Order Block obligatoire             │       │
│    │ • require_fvg: false  // Fair Value Gap optionnel          │       │
│    │ • eqh_eql_priority: true  // EQH/EQL > OB > FVG            │       │
│    │ • execution_policy: "LIMIT"  // Entrée sur pullback        │       │
│    └───────────────────────────────────────────────────────────┘       │
│                                                                          │
│ 3. Validation Phase Marché                                              │
│    IF phase_signal["phase"] NOT IN required_phase:                      │
│        RETURN {"action": "HOLD", "rule_name": "WRONG_PHASE"}            │
│                                                                          │
│ 4. Validation Confiance Phase                                           │
│    IF phase_signal["confidence"] < min_confidence:                      │
│        RETURN {"action": "HOLD", "rule_name": "LOW_PHASE_CONFIDENCE"}   │
│                                                                          │
│ 5. Détection Liquidity Sweeps                                           │
│    sweeps = phase_signal["summary"]["liquidity_sweeps"]                 │
│    IF len(sweeps) == 0:                                                 │
│        RETURN {"action": "HOLD", "rule_name": "NO_SWEEP"}               │
│                                                                          │
│ 6. Détection Order Blocks / FVG (selon config)                          │
│    order_blocks = phase_signal["summary"]["order_blocks"]               │
│    fvg_zones = phase_signal["summary"]["fvg_zones"]                     │
│                                                                          │
│    IF require_ob AND len(order_blocks) == 0:                            │
│        RETURN {"action": "HOLD", "rule_name": "NO_OB"}                  │
│                                                                          │
│ 7. Priorisation Poche Liquidité (EQH/EQL > OB > FVG)                    │
│    target_zone = None                                                   │
│    IF len(sweeps) > 0:                                                  │
│        # Sweep détecté → cible = zone opposée                           │
│        sweep = sweeps[0]  // Plus récent                                │
│        IF sweep["type"] == "EQL" (Equal Lows):                          │
│            direction = "BUY"  // Sweep baissier → rejet haussier        │
│            target_zone = order_blocks[0] if order_blocks else fvg_zones[0] │
│        ELIF sweep["type"] == "EQH" (Equal Highs):                       │
│            direction = "SELL"  // Sweep haussier → rejet baissier       │
│            target_zone = order_blocks[0] if order_blocks else fvg_zones[0] │
│                                                                          │
│ 8. Calcul SL / TP                                                       │
│    ┌───────────────────────────────────────────────────────────┐       │
│    │ SL:                                                        │       │
│    │   - Derrière extrême du sweep                              │       │
│    │   - Ex: Sweep EQL → SL = low du sweep - buffer (10 pips)   │       │
│    │   - Fallback: ATR si extrême non clair                     │       │
│    │                                                             │       │
│    │ TP:                                                        │       │
│    │   - Vers poche liquidité suivante                          │       │
│    │   - Ex: Entry depuis OB → TP = EQH suivant                 │       │
│    │   - Multi-TP possible (50% TP1, 50% TP2)                   │       │
│    └───────────────────────────────────────────────────────────┘       │
│                                                                          │
│ 9. Construction Décision                                                │
│    execution_params = {                                                 │
│        "entry_mode": "LIMIT",  // Ordre limite sur zone OB/FVG          │
│        "limit_price": target_zone["price"],                             │
│        "sl_price": calculated_sl,                                       │
│        "tp_price": calculated_tp,                                       │
│        "sl_method": "SWING",  // Derrière extrême                       │
│        "tp_method": "TARGET_ZONE",  // Poche liquidité                  │
│        "burst_enabled": false,  // Pas de burst pour liquidity          │
│    }                                                                    │
│                                                                          │
│ OUTPUT:                                                                  │
│   {                                                                     │
│     "action": "BUY" / "SELL" / "HOLD",                                  │
│     "asset": asset,                                                     │
│     "confidence": phase_signal["confidence"],                           │
│     "rule_name": "LIQUIDITY_SWAP_ENTRY",                                │
│     "execution_params": execution_params,                               │
│     "execution_status": "PENDING"                                       │
│   }                                                                     │
└─────────────────────────────────────────────────────────────────────────┘
```

---

## 6. PIPELINE D'EXÉCUTION

### 6.1 Détail Étape D: TRADE EXECUTION

```
┌─────────────────────────────────────────────────────────────────────────┐
│ D. TRADE EXECUTION & VALIDATION                                         │
│ Fichier: trader/trade_executor.py::execute_decision()                  │
│ Appelé par: run_single_pipeline_cycle()                                │
└─────────────────────────────────────────────────────────────────────────┘
                                   │
                    ┌──────────────┴──────────────┐
                    ▼                             ▼
        ┌───────────────────┐          ┌──────────────────┐
        │ D.1 Validators    │          │ D.2 OrderBuilder │
        │ (Pre-flight)      │─────────▶│ (Construction)   │
        └───────────────────┘          └──────────────────┘
                                               │
                    ┌──────────────────────────┴──────────────────────────┐
                    ▼                                                     ▼
        ┌───────────────────────────┐                  ┌──────────────────────────┐
        │ D.3 Burst Handler         │                  │ D.4 MT5 Execution        │
        │ (Si burst_enabled)        │                  │ (Send Orders)            │
        └───────────────────────────┘                  └──────────────────────────┘
                    │                                                     │
                    └──────────────────────────┬──────────────────────────┘
                                               ▼
                                ┌──────────────────────────────┐
                                │ D.5 Reconcile & Audit        │
                                │ (Post-execution)             │
                                └──────────────────────────────┘
```

#### D.1 Validators (Pre-flight Checks)

```
┌─────────────────────────────────────────────────────────────────────────┐
│ Pre-flight Validators                                                   │
│ Fichier: trader/validators.py                                          │
├─────────────────────────────────────────────────────────────────────────┤
│ INPUT: decision, account_info, symbol_info, config                      │
│                                                                          │
│ VALIDATIONS:                                                             │
│                                                                          │
│ 1. Spread Check                                                         │
│    _check_spread(symbol_info, config)                                   │
│    ┌───────────────────────────────────────────────────────────┐       │
│    │ current_spread = symbol_info.spread / 10.0  // pips        │       │
│    │ max_spread_absolute = config["max_spread_pips"]            │       │
│    │                                                             │       │
│    │ IF current_spread > max_spread_absolute:                   │       │
│    │     REJECT ("SPREAD_TOO_HIGH")                             │       │
│    │                                                             │       │
│    │ Spread dynamique (optionnel):                              │       │
│    │   - Calcul moyenne spread session (120 dernières min)      │       │
│    │   - Limite: current_spread > 1.5x moyenne                  │       │
│    └───────────────────────────────────────────────────────────┘       │
│                                                                          │
│ 2. Portfolio Exposure Check                                             │
│    _check_portfolio_exposure(decision, open_positions, config)          │
│    ┌───────────────────────────────────────────────────────────┐       │
│    │ Limites Globales:                                          │       │
│    │   - max_open_positions: 15 (toutes stratégies)             │       │
│    │   - max_risk_exposure_usd: 5000 (cumul risques SL)         │       │
│    │   - max_correlation_exposure: 0.70 (actifs corrélés)       │       │
│    │                                                             │       │
│    │ Calcul Exposition Actuelle:                                │       │
│    │   total_positions = len(open_positions)                    │       │
│    │   total_risk_usd = Somme(position.risk_usd for all)        │       │
│    │                                                             │       │
│    │ IF total_positions >= max_open_positions:                  │       │
│    │     REJECT ("MAX_POSITIONS_REACHED")                       │       │
│    │                                                             │       │
│    │ IF total_risk_usd + new_risk_usd > max_risk_exposure_usd:  │       │
│    │     REJECT ("MAX_RISK_EXPOSURE")                           │       │
│    └───────────────────────────────────────────────────────────┘       │
│                                                                          │
│ 3. Trading Hours Check                                                  │
│    _check_trading_hours(current_time, config)                           │
│    ┌───────────────────────────────────────────────────────────┐       │
│    │ Fenêtres autorisées (depuis config):                       │       │
│    │   - London Open: 08:00-12:00 UTC                           │       │
│    │   - NY Open: 13:00-17:00 UTC                               │       │
│    │   - Éviter: 22:00-01:00 UTC (liquidité faible)             │       │
│    │                                                             │       │
│    │ IF current_time NOT IN allowed_windows:                    │       │
│    │     REJECT ("OUTSIDE_TRADING_HOURS")                       │       │
│    └───────────────────────────────────────────────────────────┘       │
│                                                                          │
│ 4. Fat Finger Check (Validation ordres aberrantes)                      │
│    _check_fat_finger(decision, symbol_info, config)                     │
│    ┌───────────────────────────────────────────────────────────┐       │
│    │ • Volume > max_lots_per_order (ex: 10.0 lots)              │       │
│    │ • SL distance > max_sl_distance_pips (ex: 500 pips)        │       │
│    │ • Risk > max_risk_per_trade_percent (ex: 5%)               │       │
│    │                                                             │       │
│    │ IF anomalie détectée:                                      │       │
│    │     REJECT ("FAT_FINGER")                                  │       │
│    └───────────────────────────────────────────────────────────┘       │
│                                                                          │
│ 5. Manual Override (Optionnel)                                          │
│    manual_override_if_needed(decision)                                  │
│    ┌───────────────────────────────────────────────────────────┐       │
│    │ IF config["manual_override_enabled"]:                      │       │
│    │     - Pause exécution                                      │       │
│    │     - Affichage décision (asset, action, volume, SL/TP)    │       │
│    │     - Input utilisateur: APPROVE / REJECT / MODIFY         │       │
│    │     - Si MODIFY: ajustement SL/TP/Volume                   │       │
│    └───────────────────────────────────────────────────────────┘       │
│                                                                          │
│ OUTPUT:                                                                  │
│   validation_result: {                                                  │
│     is_valid: bool,                                                     │
│     reject_reason: str (si is_valid=False),                             │
│     warnings: [anomalies non-bloquantes]                                │
│   }                                                                     │
└─────────────────────────────────────────────────────────────────────────┘
```

#### D.2 OrderBuilder (Construction Ordres)

```
┌─────────────────────────────────────────────────────────────────────────┐
│ OrderBuilder                                                            │
│ Fichier: trader/trade_executor.py::_build_order_request()              │
├─────────────────────────────────────────────────────────────────────────┤
│ INPUT: decision, symbol_info, account_info, config                      │
│                                                                          │
│ PROCESSUS:                                                               │
│                                                                          │
│ 1. Sizing (Calcul Volume)                                               │
│    trader/sizing.py::_calculate_risk_based_volume()                     │
│    ┌───────────────────────────────────────────────────────────┐       │
│    │ INPUT:                                                     │       │
│    │   - action: "BUY" / "SELL"                                 │       │
│    │   - entry_price: Prix entrée estimé (bid/ask actuel)       │       │
│    │   - sl_price: Stop Loss calculé (étape suivante)           │       │
│    │   - equity: Capital compte (account_info.equity)           │       │
│    │   - risk_per_trade_percent: 1.0% (depuis config)           │       │
│    │   - asset: "XAUUSD" / etc.                                 │       │
│    │                                                             │       │
│    │ FORMULE:                                                   │       │
│    │   distance_price = abs(entry_price - sl_price)             │       │
│    │   risk_amount_usd = equity × (risk_percent / 100)          │       │
│    │   point_value = symbol_info.point_value  // $ par pip/point│       │
│    │   volume = (risk_amount_usd / distance_price) / point_value│       │
│    │   volume_floor = FLOOR(volume / lot_step) × lot_step       │       │
│    │                                                             │       │
│    │ CONTRAINTES:                                               │       │
│    │   - volume >= symbol_info.volume_min (ex: 0.01 lots)       │       │
│    │   - volume <= symbol_info.volume_max (ex: 50.0 lots)       │       │
│    │   - volume % symbol_info.volume_step == 0 (ex: 0.01 step)  │       │
│    │   - NEVER au-dessus budget (FLOOR strict)                  │       │
│    │                                                             │       │
│    │ FALLBACK:                                                  │       │
│    │   IF equity manquant:                                      │       │
│    │     equity = env.SNIPERX_DEFAULT_EQUITY (10000 USD)        │       │
│    │                                                             │       │
│    │ OUTPUT:                                                    │       │
│    │   volume: float (ex: 0.12 lots)                            │       │
│    └───────────────────────────────────────────────────────────┘       │
│                                                                          │
│ 2. SLTP Calculation                                                     │
│    trader/sltp.py::_calculate_sl_tp_prices()                            │
│    ┌───────────────────────────────────────────────────────────┐       │
│    │ INPUT:                                                     │       │
│    │   - action: "BUY" / "SELL"                                 │       │
│    │   - entry_price: Prix entrée                               │       │
│    │   - sl_method: "PIPS" / "ATR" / "SWING"                    │       │
│    │   - tp_method: "PIPS" / "RR" / None                        │       │
│    │   - df_m5: DataFrame pour calculs (ATR, swings)            │       │
│    │   - config: sl_pips, tp_pips, atr_period, rr_ratio         │       │
│    │                                                             │       │
│    │ MÉTHODES SL:                                               │       │
│    │ ├─ PIPS:                                                   │       │
│    │ │    sl_price = entry ± (sl_pips × point_size)             │       │
│    │ │    Ex BUY: sl_price = 2050.50 - (30 × 0.01) = 2050.20    │       │
│    │ │                                                           │       │
│    │ ├─ ATR:                                                    │       │
│    │ │    atr = ATR(df_m5, period=14)  // TrueRange moyenne     │       │
│    │ │    sl_distance = k × atr  (k=1.5 depuis config)          │       │
│    │ │    sl_price = entry ± sl_distance                        │       │
│    │ │    → Adaptatif à volatilité                              │       │
│    │ │                                                           │       │
│    │ └─ SWING:                                                  │       │
│    │      swings = find_swing_points(df_m5, lookback=20)        │       │
│    │      Ex BUY: sl_price = last_swing_low - buffer (10 pips)  │       │
│    │      Ex SELL: sl_price = last_swing_high + buffer          │       │
│    │      → Derrière extrêmes structurels                       │       │
│    │                                                             │       │
│    │ MÉTHODES TP:                                               │       │
│    │ ├─ PIPS:                                                   │       │
│    │ │    tp_price = entry ± (tp_pips × point_size)             │       │
│    │ │                                                           │       │
│    │ ├─ RR (Risk-Reward):                                       │       │
│    │ │    sl_distance = abs(entry - sl_price)                   │       │
│    │ │    tp_distance = sl_distance × rr_ratio  (ex: 2.5)       │       │
│    │ │    tp_price = entry ± tp_distance                        │       │
│    │ │    → TP adapté au SL (ratio constant)                    │       │
│    │ │                                                           │       │
│    │ └─ None:                                                   │       │
│    │      tp_price = None  // Pas de TP (scalping trailing)     │       │
│    │                                                             │       │
│    │ MULTI-TP (Optionnel):                                      │       │
│    │   IF config["use_multiple_tp"]:                            │       │
│    │     tp1 = entry + (tp_distance × 0.5)  // 50% position     │       │
│    │     tp2 = entry + (tp_distance × 1.0)  // 50% restant      │       │
│    │     → Split order en 2 tickets                             │       │
│    │                                                             │       │
│    │ OUTPUT:                                                    │       │
│    │   {                                                        │       │
│    │     sl_price: float,                                       │       │
│    │     tp_price: float or None,                               │       │
│    │     tp_prices: [tp1, tp2] if multi-TP,                     │       │
│    │     reason: "ATR_1.5x" / "SWING_LOW" / "RR_2.5",           │       │
│    │     rr_achieved: float (si TP défini)                      │       │
│    │   }                                                        │       │
│    └───────────────────────────────────────────────────────────┘       │
│                                                                          │
│ 3. Magic Number Assignment                                              │
│    magic_number = strategy_config["magic_number"]                       │
│    → 52001 (Scalping) ou 53001 (Liquidity)                              │
│                                                                          │
│ 4. Comment Metadata                                                     │
│    comment = f"SNIPERX_{strategy_name}_{cycle}_{timestamp}"             │
│    → "SNIPERX_SCALPING_42_20251206143200"                               │
│                                                                          │
│ 5. Construction MT5 Request                                             │
│    order_request = {                                                    │
│        "action": mt5.TRADE_ACTION_DEAL,  // Ordre marché immédiat       │
│        "symbol": asset,  // "XAUUSD"                                    │
│        "volume": volume,  // 0.12 lots (calculé étape 1)                │
│        "type": mt5.ORDER_TYPE_BUY if action=="BUY" else ORDER_TYPE_SELL,│
│        "price": entry_price,  // Bid (SELL) ou Ask (BUY)                │
│        "sl": sl_price,  // Stop Loss                                    │
│        "tp": tp_price,  // Take Profit (ou None)                        │
│        "deviation": 10,  // Slippage max (pips)                         │
│        "magic": magic_number,  // 52001 / 53001                         │
│        "comment": comment,  // Métadonnées                              │
│        "type_time": mt5.ORDER_TIME_GTC,  // Good Till Cancelled         │
│        "type_filling": mt5.ORDER_FILLING_IOC,  // Immediate Or Cancel   │
│    }                                                                    │
│                                                                          │
│ OUTPUT:                                                                  │
│   order_request: Dict MT5 (prêt pour mt5.order_send())                  │
└─────────────────────────────────────────────────────────────────────────┘
```

#### D.3 BurstHandler (Gestion Paniers Scalping)

```
┌─────────────────────────────────────────────────────────────────────────┐
│ BurstHandler - Gestion Baskets Burst                                   │
│ Fichier: trader/trade_executor.py::open_burst_basket()                 │
├─────────────────────────────────────────────────────────────────────────┤
│ INPUT:                                                                   │
│   - order_request: Dict MT5 (from OrderBuilder)                         │
│   - burst_size: 8 (nombre tickets dans panier)                          │
│   - config: Configuration burst (SL commun, monitoring)                 │
│                                                                          │
│ PROCESSUS:                                                               │
│                                                                          │
│ 1. Génération Basket ID                                                 │
│    basket_id = f"BURST_{asset}_{timestamp}_{random_suffix}"             │
│    → "BURST_XAUUSD_20251206143200_A7F2"                                 │
│                                                                          │
│ 2. Division Volume                                                      │
│    ┌───────────────────────────────────────────────────────────┐       │
│    │ volume_total = order_request["volume"]  // 0.12 lots       │       │
│    │ volume_per_ticket = volume_total / burst_size              │       │
│    │ → 0.12 / 8 = 0.015 lots par ticket                         │       │
│    │                                                             │       │
│    │ Arrondi au lot_step:                                       │       │
│    │   volume_per_ticket = round(0.015 / 0.01) × 0.01 = 0.01    │       │
│    │   → Chaque ticket: 0.01 lots                               │       │
│    └───────────────────────────────────────────────────────────┘       │
│                                                                          │
│ 3. SL/TP Commun vs Individuel                                           │
│    ┌───────────────────────────────────────────────────────────┐       │
│    │ IF config["burst_common_sl"]:                              │       │
│    │     sl_price = order_request["sl"]  // Tous même SL        │       │
│    │     tp_price = None  // Pas de TP (trailing global)        │       │
│    │ ELSE:                                                      │       │
│    │     # SL/TP individuel par ticket (rare)                   │       │
│    │     sl_price = calculé par ticket                          │       │
│    └───────────────────────────────────────────────────────────┘       │
│                                                                          │
│ 4. Envoi Séquentiel Tickets                                             │
│    tickets = []                                                         │
│    for i in range(burst_size):                                          │
│        ┌───────────────────────────────────────────────────────┐       │
│        │ ticket_request = {                                     │       │
│        │     ...order_request,  // Copy base request            │       │
│        │     "volume": volume_per_ticket,                       │       │
│        │     "comment": f"{comment}_BURST_{basket_id}_{i+1}"    │       │
│        │ }                                                      │       │
│        │                                                         │       │
│        │ result = mt5.order_send(ticket_request)                │       │
│        │ IF result.retcode == mt5.TRADE_RETCODE_DONE:           │       │
│        │     tickets.append({                                   │       │
│        │         "ticket": result.order,                        │       │
│        │         "entry_price": result.price,                   │       │
│        │         "volume": volume_per_ticket,                   │       │
│        │         "basket_id": basket_id                         │       │
│        │     })                                                 │       │
│        │     logger.info(f"Ticket {i+1}/{burst_size} OK")       │       │
│        │ ELSE:                                                  │       │
│        │     logger.error(f"Ticket {i+1} FAILED: {result.retcode}") │  │
│        │     # Continue tentative autres tickets                │       │
│        └───────────────────────────────────────────────────────┘       │
│                                                                          │
│ 5. Enregistrement Basket                                                │
│    ┌───────────────────────────────────────────────────────────┐       │
│    │ self._burst_baskets[basket_id] = {                         │       │
│    │     "tickets": tickets,                                    │       │
│    │     "asset": asset,                                        │       │
│    │     "direction": action,  // "BUY" / "SELL"                │       │
│    │     "entry_avg": avg([t["entry_price"] for t in tickets]), │       │
│    │     "sl_common": sl_price,                                 │       │
│    │     "created_at": datetime.now(UTC),                       │       │
│    │     "status": "ACTIVE",                                    │       │
│    │     "profit_target_pips": 15,  // +15 pips → close all     │       │
│    │     "loss_guard_pips": 110,  // -110 pips → emergency stop │       │
│    │     "metadata": {                                          │       │
│    │         "strategy": "scalping",                            │       │
│    │         "confidence": fused_confidence,                    │       │
│    │         "cycle": cycle_count                               │       │
│    │     }                                                      │       │
│    │ }                                                          │       │
│    └───────────────────────────────────────────────────────────┘       │
│                                                                          │
│ 6. Alerte Telegram                                                      │
│    config_manager.send_alert(                                           │
│        f"🎯 BURST BASKET OPENED\n"                                      │
│        f"Asset: {asset}\n"                                              │
│        f"Direction: {action}\n"                                         │
│        f"Tickets: {len(tickets)}/{burst_size}\n"                        │
│        f"Entry Avg: {entry_avg}\n"                                      │
│        f"SL: {sl_price}\n"                                              │
│        f"Basket ID: {basket_id}"                                        │
│    )                                                                    │
│                                                                          │
│ OUTPUT:                                                                  │
│   basket_result: {                                                      │
│     success: bool,                                                      │
│     basket_id: str,                                                     │
│     tickets_opened: int,                                                │
│     tickets_failed: int,                                                │
│     entry_avg: float                                                    │
│   }                                                                     │
└─────────────────────────────────────────────────────────────────────────┘
```

#### D.4 MT5 Execution (Envoi Ordres)

```
┌─────────────────────────────────────────────────────────────────────────┐
│ MT5Connector.order_send()                                               │
│ Fichier: mt5_connector.py::execute_order()                             │
├─────────────────────────────────────────────────────────────────────────┤
│ INPUT: order_request (Dict MT5 formaté)                                 │
│                                                                          │
│ PROCESSUS:                                                               │
│                                                                          │
│ 1. Vérification Connexion MT5                                           │
│    IF not self.is_connected:                                            │
│        logger.error("MT5 not connected")                                │
│        RETURN {"retcode": TRADE_RETCODE_CONNECTION_ERROR}               │
│                                                                          │
│ 2. Validation Symbol                                                    │
│    symbol_info = mt5.symbol_info(order_request["symbol"])               │
│    IF symbol_info is None:                                              │
│        RETURN {"retcode": TRADE_RETCODE_INVALID_SYMBOL}                 │
│                                                                          │
│ 3. Préparation Ordre                                                    │
│    ┌───────────────────────────────────────────────────────────┐       │
│    │ # MT5 exige tuple/namedtuple pour order_send()            │       │
│    │ from collections import namedtuple                         │       │
│    │ OrderRequest = namedtuple('OrderRequest', [                │       │
│    │     'action', 'symbol', 'volume', 'type', 'price',         │       │
│    │     'sl', 'tp', 'deviation', 'magic', 'comment',           │       │
│    │     'type_time', 'type_filling'                            │       │
│    │ ])                                                         │       │
│    │                                                             │       │
│    │ request = OrderRequest(**order_request)                    │       │
│    └───────────────────────────────────────────────────────────┘       │
│                                                                          │
│ 4. Envoi Ordre MT5                                                      │
│    ┌───────────────────────────────────────────────────────────┐       │
│    │ result = mt5.order_send(request)                           │       │
│    │                                                             │       │
│    │ # Result structure:                                        │       │
│    │ # - retcode: Code retour (10009=SUCCESS, autres=ERRORS)    │       │
│    │ # - order: Ticket number (si succès)                       │       │
│    │ # - volume: Volume exécuté                                 │       │
│    │ # - price: Prix exécution réel                             │       │
│    │ # - bid/ask: Prix marché au moment exécution               │       │
│    │ # - comment: Commentaire broker                            │       │
│    │ # - request_id: ID requête                                 │       │
│    └───────────────────────────────────────────────────────────┘       │
│                                                                          │
│ 5. Gestion Retcodes                                                     │
│    ┌───────────────────────────────────────────────────────────┐       │
│    │ RETCODE MAPPING:                                           │       │
│    │ ├─ 10009 TRADE_RETCODE_DONE → Succès                      │       │
│    │ ├─ 10004 TRADE_RETCODE_REQUOTE → Slippage, retry          │       │
│    │ ├─ 10006 TRADE_RETCODE_REJECT → Rejeté broker             │       │
│    │ ├─ 10007 TRADE_RETCODE_CANCEL → Annulé                    │       │
│    │ ├─ 10013 TRADE_RETCODE_INVALID_VOLUME → Volume invalide   │       │
│    │ ├─ 10014 TRADE_RETCODE_INVALID_PRICE → Prix invalide      │       │
│    │ ├─ 10015 TRADE_RETCODE_INVALID_STOPS → SL/TP invalide     │       │
│    │ ├─ 10018 TRADE_RETCODE_MARKET_CLOSED → Marché fermé       │       │
│    │ ├─ 10019 TRADE_RETCODE_NO_MONEY → Marge insuffisante      │       │
│    │ └─ 10027 TRADE_RETCODE_TIMEOUT → Timeout                  │       │
│    │                                                             │       │
│    │ IF result.retcode == TRADE_RETCODE_DONE:                   │       │
│    │     logger.info(f"Order SUCCESS: Ticket {result.order}")   │       │
│    │     self._last_order_ticket = result.order                 │       │
│    │ ELSE:                                                      │       │
│    │     logger.error(f"Order FAILED: {result.retcode} - {result.comment}") │
│    └───────────────────────────────────────────────────────────┘       │
│                                                                          │
│ 6. Retry Logic (Requote uniquement)                                     │
│    ┌───────────────────────────────────────────────────────────┐       │
│    │ IF result.retcode == TRADE_RETCODE_REQUOTE:                │       │
│    │     # Prix a changé entre validation et exécution          │       │
│    │     new_price = result.bid if action=="SELL" else result.ask│      │
│    │     IF abs(new_price - request.price) <= deviation:        │       │
│    │         # Retry avec nouveau prix si dans tolérance        │       │
│    │         request = request._replace(price=new_price)        │       │
│    │         result = mt5.order_send(request)  // 1 retry max   │       │
│    └───────────────────────────────────────────────────────────┘       │
│                                                                          │
│ 7. Logging Détaillé                                                     │
│    logger.debug(f"Order sent: {request}")                               │
│    logger.info(f"Result: retcode={result.retcode}, ticket={result.order}, "│
│                f"price={result.price}, comment={result.comment}")       │
│                                                                          │
│ OUTPUT:                                                                  │
│   result: MT5 OrderSendResult namedtuple                                │
│   {                                                                     │
│     retcode: int,                                                       │
│     order: int (ticket),                                                │
│     volume: float,                                                      │
│     price: float,                                                       │
│     bid: float,                                                         │
│     ask: float,                                                         │
│     comment: str                                                        │
│   }                                                                     │
└─────────────────────────────────────────────────────────────────────────┘
```

#### D.5 Reconcile & Audit (Post-Execution)

```
┌─────────────────────────────────────────────────────────────────────────┐
│ Reconciliation & Audit Trail                                           │
│ Fichier: trader/reconcile.py & core/audit_logger.py                    │
├─────────────────────────────────────────────────────────────────────────┤
│ PROCESSUS:                                                               │
│                                                                          │
│ 1. Mise à Jour Cache Interne                                            │
│    trader/trade_executor.py::_update_internal_state()                   │
│    ┌───────────────────────────────────────────────────────────┐       │
│    │ IF order SUCCESS:                                          │       │
│    │     self._open_positions[ticket] = {                       │       │
│    │         "ticket": result.order,                            │       │
│    │         "asset": asset,                                    │       │
│    │         "type": action,  // "BUY" / "SELL"                 │       │
│    │         "volume": volume,                                  │       │
│    │         "entry_price": result.price,                       │       │
│    │         "sl": sl_price,                                    │       │
│    │         "tp": tp_price,                                    │       │
│    │         "magic": magic_number,                             │       │
│    │         "strategy": strategy_name,                         │       │
│    │         "basket_id": basket_id (if burst),                 │       │
│    │         "opened_at": datetime.now(UTC),                    │       │
│    │         "profit": 0.0,  // Mis à jour par monitoring       │       │
│    │     }                                                      │       │
│    └───────────────────────────────────────────────────────────┘       │
│                                                                          │
│ 2. Réconciliation avec Broker                                           │
│    trader/reconcile.py::reconcile_state_with_broker()                   │
│    ┌───────────────────────────────────────────────────────────┐       │
│    │ # Source de vérité: Broker MT5                             │       │
│    │ broker_positions = mt5.positions_get()                     │       │
│    │                                                             │       │
│    │ # Synchronisation bidirectionnelle:                        │       │
│    │ FOR each broker_pos IN broker_positions:                   │       │
│    │     IF broker_pos.ticket NOT IN self._open_positions:      │       │
│    │         # Position ouverte hors bot → importer             │       │
│    │         self._open_positions[ticket] = from_mt5(broker_pos)│       │
│    │         logger.warning(f"Position {ticket} importée")      │       │
│    │                                                             │       │
│    │ FOR each ticket IN self._open_positions:                   │       │
│    │     IF ticket NOT IN broker_positions:                     │       │
│    │         # Position fermée côté broker → purger             │       │
│    │         del self._open_positions[ticket]                   │       │
│    │         logger.info(f"Position {ticket} purgée (closed)")  │       │
│    │                                                             │       │
│    │ # Mise à jour profits                                      │       │
│    │ FOR each pos IN broker_positions:                          │       │
│    │     self._open_positions[pos.ticket]["profit"] = pos.profit│       │
│    │                                                             │       │
│    │ self._last_reconciliation_time = datetime.now(UTC)         │       │
│    └───────────────────────────────────────────────────────────┘       │
│                                                                          │
│ 3. Audit Logging                                                        │
│    core/audit_logger.py::log_trade_execution()                          │
│    ┌───────────────────────────────────────────────────────────┐       │
│    │ audit_entry = {                                            │       │
│    │     "timestamp": datetime.now(UTC).isoformat(),            │       │
│    │     "event_type": "TRADE_EXECUTION",                       │       │
│    │     "asset": asset,                                        │       │
│    │     "action": action,                                      │       │
│    │     "ticket": result.order,                                │       │
│    │     "entry_price": result.price,                           │       │
│    │     "volume": volume,                                      │       │
│    │     "sl": sl_price,                                        │       │
│    │     "tp": tp_price,                                        │       │
│    │     "strategy": strategy_name,                             │       │
│    │     "magic": magic_number,                                 │       │
│    │     "basket_id": basket_id,                                │       │
│    │     "confidence": fused_confidence,                        │       │
│    │     "signal_type": signal_type,                            │       │
│    │     "context": {                                           │       │
│    │         "cycle": cycle_count,                              │       │
│    │         "market_phase": phase_signal["phase"],             │       │
│    │         "vwap_regime": vwap_regime,                        │       │
│    │         "orderflow_score": orderflow_score,                │       │
│    │         "footprint_score": footprint_score,                │       │
│    │         "vwap_score": vwap_score                           │       │
│    │     },                                                     │       │
│    │     "execution_details": {                                 │       │
│    │         "retcode": result.retcode,                         │       │
│    │         "comment": result.comment,                         │       │
│    │         "slippage": result.price - expected_price,         │       │
│    │         "spread_at_execution": symbol_info.spread          │       │
│    │     }                                                      │       │
│    │ }                                                          │       │
│    │                                                             │       │
│    │ # Écriture fichier logs/audit_{date}.log (JSON Lines)      │       │
│    │ with open(audit_file, 'a') as f:                           │       │
│    │     f.write(json.dumps(audit_entry) + '\n')                │       │
│    └───────────────────────────────────────────────────────────┘       │
│                                                                          │
│ 4. Trade Logging (Fichier dédié)                                        │
│    trader/trade_logger.py::log_execution()                              │
│    ┌───────────────────────────────────────────────────────────┐       │
│    │ # Fichier: logs/trades_{date}.log                          │       │
│    │ log_line = (                                               │       │
│    │     f"{timestamp} | {asset} | {action} | "                 │       │
│    │     f"Ticket: {ticket} | Entry: {entry_price} | "          │       │
│    │     f"SL: {sl_price} | Volume: {volume} | "                │       │
│    │     f"Strategy: {strategy_name} | Confidence: {confidence}"│       │
│    │ )                                                          │       │
│    │ trade_logger.info(log_line)                                │       │
│    └───────────────────────────────────────────────────────────┘       │
│                                                                          │
│ OUTPUT:                                                                  │
│   - Cache interne à jour                                                │
│   - Audit trail complet (JSON)                                          │
│   - Logs lisibles (texte)                                               │
│   - Réconciliation timestamp enregistré                                 │
└─────────────────────────────────────────────────────────────────────────┘
```

---

## 7. CYCLE DE VIE COMPLET D'UN TRADE

### 7.1 Exemple Concret: Trade XAUUSD Scalping Burst

```
┌─────────────────────────────────────────────────────────────────────────┐
│ CYCLE DE VIE COMPLET - TRADE XAUUSD SCALPING BURST                     │
│ De la détection du signal à la fermeture avec profit                   │
└─────────────────────────────────────────────────────────────────────────┘

════════════════════════════════════════════════════════════════════════
PHASE 1: DÉTECTION SIGNAL (Cycle #42 - 14:32:00 UTC)
════════════════════════════════════════════════════════════════════════

[14:32:00] Cycle démarre, récupération données marché
           ├─ XAUUSD M1: 120 bougies
           ├─ XAUUSD M5: 240 bougies
           ├─ XAUUSD M15: 240 bougies
           └─ Ticks M1: 387 ticks (coverage: 58s)

[14:32:03] OrderFlow V6 Analysis
           ├─ CVD slope: +2.8 (momentum haussier)
           ├─ Delta total: +1842 (pression acheteuse)
           ├─ Imbalance: 0.68 (68% buy volume)
           ├─ VPOC: 2050.30 (prix institutionnel)
           ├─ Pattern détecté: "volume_breakout" (conf: 0.85)
           └─ Score: 78/100, Status: VALID, Bias: BUY

[14:32:04] Footprint M1 Analysis
           ├─ Buy volume: 8420, Sell volume: 4380
           ├─ Delta total: +4040
           ├─ POC: 2050.35
           ├─ Tick rate: 6.7 ticks/s (activité soutenue)
           └─ Score: 65/100, Status: VALID

[14:32:05] VWAP Analysis
           ├─ VWAP value: 2048.30
           ├─ Distance: +22.0 pips (prix > VWAP)
           ├─ Slope: +0.012 (tendance haussière)
           ├─ Regime: TRENDING
           ├─ Signal: "proximity" (proche VWAP, mean reversion possible)
           └─ Score: 0.82/1.0, Status: VALID, Bias: BULLISH

[14:32:06] FusionManager - Fusion Signaux
           ├─ Normalisation:
           │  ├─ OrderFlow: score=0.78, dir=+1
           │  ├─ Footprint: score=0.65, dir=+1
           │  └─ VWAP: score=0.82, dir=+1
           │
           ├─ Cohérence:
           │  ├─ Votes: [("orderflow", "BUY", 0.78), ("footprint", "BUY", 0.65), ("vwap", "BUY", 0.82)]
           │  ├─ Majorité: BUY (+1)
           │  ├─ Agreement: 0.95 (95% consensus)
           │  └─ Matrice: of_vs_fp=aligned, of_vs_vwap=aligned, fp_vs_vwap=aligned
           │
           ├─ Poids Adaptatifs (VWAP Regime = TRENDING):
           │  ├─ VWAP: 50% (prioritaire en trending)
           │  ├─ OrderFlow: 30%
           │  └─ Footprint: 20%
           │
           ├─ Formule:
           │  weighted_score = (0.78×0.30) + (0.65×0.20) + (0.82×0.50)
           │                 = 0.234 + 0.130 + 0.410 = 0.774
           │
           ├─ Bonus Cohérence:
           │  └─ Alignement 3/3: +0.05 → 0.774 + 0.05 = 0.824
           │
           └─ Décision: fused_confidence=0.82 → SIGNAL_TYPE: HIGH_CONVICTION_BUY

════════════════════════════════════════════════════════════════════════
PHASE 2: DÉCISION STRATÉGIE (14:32:07)
════════════════════════════════════════════════════════════════════════

[14:32:07] DecisionPipeline - Sélection Stratégie
           ├─ Asset config: XAUUSD.json
           ├─ Strategy name: "scalping"
           └─ Appel ScalpingPipeline.run()

[14:32:08] ScalpingStrategy.evaluate_entry()
           ├─ Entry rules extraction:
           │  ├─ min_confidence: 0.55 ✓ (0.82 >= 0.55)
           │  ├─ burst_enabled: true
           │  ├─ burst_size: 8
           │  └─ max_spread_pips: 2.0
           │
           ├─ Validation Spread:
           │  └─ Current spread: 1.2 pips ✓ (< 2.0)
           │
           ├─ Validation Consensus:
           │  └─ Direction: BUY ✓ (majorité claire)
           │
           └─ Décision: ACTION=BUY, CONFIDENCE=0.82
              execution_params: {
                  burst_enabled: true,
                  burst_size: 8,
                  entry_mode: "MARKET",
                  sl_method: "ATR",
                  tp_method: None (trailing only)
              }

════════════════════════════════════════════════════════════════════════
PHASE 3: VALIDATION PRÉ-EXÉCUTION (14:32:09)
════════════════════════════════════════════════════════════════════════

[14:32:09] Validators - Pre-flight Checks
           ├─ ✓ Spread check: 1.2 pips < 2.0 (OK)
           ├─ ✓ Portfolio exposure: 8 positions / 15 max (OK)
           ├─ ✓ Risk exposure: $1200 / $5000 max (OK)
           ├─ ✓ Trading hours: 14:32 in [08:00-17:00] (OK)
           └─ ✓ Fat finger check: Volume 0.12 lots < 10.0 max (OK)

           Validation: ALL CHECKS PASSED ✅

════════════════════════════════════════════════════════════════════════
PHASE 4: CONSTRUCTION ORDRE (14:32:10)
════════════════════════════════════════════════════════════════════════

[14:32:10] OrderBuilder - Sizing
           ├─ Equity: $10,000
           ├─ Risk per trade: 1.0% → $100
           ├─ Entry price (Ask): 2050.50
           ├─ SL calculation (ATR method):
           │  ├─ ATR(14): 8.5 pips
           │  ├─ k=1.5 → SL distance = 12.75 pips
           │  └─ SL price = 2050.50 - 0.1275 = 2050.3725
           ├─ Distance: 12.75 pips = $12.75 par lot
           ├─ Volume = $100 / $12.75 = 7.84 lots
           ├─ Floor to lot_step (0.01): 7.84 → 7.84 lots
           └─ Volume final: 7.84 lots (TOTAL pour burst)

[14:32:11] OrderBuilder - SLTP
           ├─ SL: 2050.3725 (ATR 1.5x, -12.75 pips)
           ├─ TP: None (scalping trailing, pas de TP fixe)
           └─ RR achieved: N/A (no TP)

[14:32:12] BurstHandler - Division Panier
           ├─ Burst size: 8 tickets
           ├─ Volume per ticket: 7.84 / 8 = 0.98 lots
           ├─ Round to lot_step: 0.98 → 0.98 lots
           ├─ SL commun: 2050.3725 (tous tickets)
           └─ Basket ID: BURST_XAUUSD_20251206143212_A7F2

════════════════════════════════════════════════════════════════════════
PHASE 5: EXÉCUTION MT5 (14:32:13 - 14:32:18)
════════════════════════════════════════════════════════════════════════

[14:32:13] Ticket 1/8
           ├─ Request: BUY 0.98 XAUUSD @ 2050.50, SL=2050.3725
           ├─ mt5.order_send() → retcode: 10009 (DONE)
           └─ Ticket: 123456781, Entry: 2050.51 (slippage +0.01)

[14:32:14] Ticket 2/8
           └─ Ticket: 123456782, Entry: 2050.50

[14:32:15] Ticket 3/8
           └─ Ticket: 123456783, Entry: 2050.52

[14:32:16] Ticket 4/8
           └─ Ticket: 123456784, Entry: 2050.51

[14:32:17] Ticket 5/8
           └─ Ticket: 123456785, Entry: 2050.50

[14:32:17] Ticket 6/8
           └─ Ticket: 123456786, Entry: 2050.51

[14:32:18] Ticket 7/8
           └─ Ticket: 123456787, Entry: 2050.50

[14:32:18] Ticket 8/8
           └─ Ticket: 123456788, Entry: 2050.52

[14:32:19] Burst Basket Summary
           ├─ Tickets opened: 8/8 ✅
           ├─ Entry average: 2050.51
           ├─ Total volume: 7.84 lots
           ├─ SL common: 2050.3725 (-13.9 pips from avg)
           └─ Status: ACTIVE

[14:32:20] 📱 Telegram Alert
           🎯 BURST BASKET OPENED
           Asset: XAUUSD
           Direction: BUY
           Tickets: 8/8
           Entry Avg: 2050.51
           SL: 2050.3725
           Basket ID: BURST_XAUUSD_20251206143212_A7F2

════════════════════════════════════════════════════════════════════════
PHASE 6: MONITORING (14:32:20 - 14:47:30, cycles #42-#57)
════════════════════════════════════════════════════════════════════════

[14:33:00] Cycle #43 - Monitoring Basket
           ├─ Current price: 2050.68 (+17 pips from avg)
           ├─ Unrealized P&L: +$133 (+1.33%)
           ├─ Profit target: +15 pips → NOT YET (17 > 15 mais basket intact)
           └─ Status: HOLDING

[14:34:00] Cycle #44
           ├─ Current price: 2050.82 (+31 pips)
           ├─ P&L: +$243
           └─ Status: HOLDING (profit monte)

[14:35:00] Cycle #45
           ├─ Current price: 2050.95 (+44 pips)
           ├─ P&L: +$345
           └─ Status: HOLDING

[14:36:00] Cycle #46
           ├─ Current price: 2051.12 (+61 pips) 📈
           ├─ P&L: +$478
           └─ Status: HOLDING (tendance forte)

... (cycles #47-#56 omis, prix oscille entre 2051.00-2051.20)

[14:47:00] Cycle #57 - Profit Target Check
           ├─ Current price: 2050.66 (+15 pips exactly!)
           ├─ P&L: +$118
           ├─ Profit target atteint: +15 pips ✓
           └─ DÉCISION: FERMETURE BASKET

════════════════════════════════════════════════════════════════════════
PHASE 7: FERMETURE BASKET (14:47:01)
════════════════════════════════════════════════════════════════════════

[14:47:01] BurstHandler.close_basket()
           ├─ Basket ID: BURST_XAUUSD_20251206143212_A7F2
           ├─ Raison: PROFIT_TARGET_REACHED (+15 pips)
           └─ Fermeture séquentielle 8 tickets...

[14:47:02] Close Ticket 123456781
           ├─ Close price: 2050.66
           ├─ Profit: +$14.70 (+15 pips)
           └─ retcode: 10009 (DONE)

[14:47:03] Close Ticket 123456782
           └─ Profit: +$15.68 (+16 pips)

... (tickets 3-7 omis)

[14:47:06] Close Ticket 123456788
           └─ Profit: +$13.72 (+14 pips)

[14:47:07] Basket Closed Summary
           ├─ Tickets closed: 8/8 ✅
           ├─ Total profit: +$118.40
           ├─ Avg profit per ticket: +$14.80
           ├─ Duration: 15 minutes
           └─ Final P&L: +1.18% (sur $10,000)

[14:47:08] 📱 Telegram Alert
           ✅ BURST BASKET CLOSED
           Basket ID: BURST_XAUUSD_20251206143212_A7F2
           Raison: PROFIT_TARGET_REACHED
           Profit Total: +$118.40
           Duration: 15 min
           Return: +1.18%

════════════════════════════════════════════════════════════════════════
PHASE 8: AUDIT & RECONCILIATION (14:47:09)
════════════════════════════════════════════════════════════════════════

[14:47:09] Audit Logger - Trade Closed
           ├─ Event: BASKET_CLOSED
           ├─ Timestamp: 2025-12-06T14:47:09Z
           ├─ Basket ID: BURST_XAUUSD_20251206143212_A7F2
           ├─ Tickets: [123456781...123456788]
           ├─ Entry avg: 2050.51
           ├─ Exit avg: 2050.66
           ├─ Profit: +$118.40
           ├─ Duration: 15 min
           └─ Reason: PROFIT_TARGET_REACHED

[14:47:10] Reconciliation avec Broker
           ├─ Récupération positions MT5: 0 (basket fermé)
           ├─ Purge cache interne: 8 tickets supprimés
           ├─ Update account_info:
           │  ├─ Balance: $10,118.40 (was $10,000)
           │  └─ Equity: $10,118.40
           └─ Reconciliation OK ✅

[14:47:11] Logs Finaux
           ├─ logs/trades_20251206.log:
           │  "14:47:09 | XAUUSD | CLOSE_BASKET | Profit: +$118.40 | Duration: 15min"
           │
           └─ logs/audit_20251206.log:
              {"timestamp": "2025-12-06T14:47:09Z", "event": "BASKET_CLOSED",
               "basket_id": "BURST_XAUUSD_20251206143212_A7F2",
               "profit_usd": 118.40, "profit_pct": 1.18, ...}

════════════════════════════════════════════════════════════════════════
FIN DU CYCLE - BILAN TRADE
════════════════════════════════════════════════════════════════════════

✅ Trade réussi:
   - Durée: 15 minutes (14:32 → 14:47)
   - Profit: +$118.40 (+1.18% capital)
   - Risk engagé: $100 (1% capital)
   - Risk/Reward achieved: 1.18:1
   - Slippage total: +0.01 pips (négligeable)
   - Tickets executés: 8/8 (100%)
   - Raison fermeture: Profit target atteint (+15 pips)

📊 Métriques Stratégie:
   - Signal confidence: 0.82 (HIGH_CONVICTION)
   - Consensus: 95% (3/3 signaux alignés)
   - VWAP regime: TRENDING (poids 50%)
   - OrderFlow score: 78/100 (VALID)
   - Footprint score: 65/100 (VALID)
   - Spread execution: 1.2 pips (< 2.0 limit)

🔄 Cycles impliqués:
   - Cycle #42: Détection signal + Exécution
   - Cycles #43-#56: Monitoring (14 cycles)
   - Cycle #57: Fermeture profit target

📝 Audit Trail:
   - Tous événements loggés (JSON + texte)
   - Réconciliation broker OK
   - Alertes Telegram envoyées (2)
```

### 7.2 Scénario Alternatif: Loss Guard (-110 pips)

```
════════════════════════════════════════════════════════════════════════
SCÉNARIO: MOUVEMENT ADVERSE - LOSS GUARD TRIGGERED
════════════════════════════════════════════════════════════════════════

[15:12:00] Cycle #82 - Trade ouvert (même setup que précédent)
           Entry avg: 2048.30, SL: 2048.1725 (-12.75 pips ATR)

[15:15:00] Prix descend: 2047.80 (-50 pips)
           P&L: -$392 (douloureux mais dans tolérance)

[15:18:00] Prix descend encore: 2047.20 (-110 pips)
           P&L: -$862
           ⚠️ LOSS GUARD TRIGGERED: -110 pips atteints

[15:18:01] Emergency Close Basket
           ├─ Raison: LOSS_GUARD_STOP (-110 pips)
           ├─ Fermeture immédiate 8 tickets
           └─ Loss total: -$862 (-8.62% capital)

[15:18:05] 🚨 Telegram Alert
           ❌ BURST BASKET EMERGENCY CLOSE
           Raison: LOSS_GUARD_TRIGGERED
           Loss: -$862 (-8.62%)
           Note: SL normal (-12.75 pips) pas touché,
                 mais loss guard (-110 pips) activé

📊 Analyse Post-Mortem:
   - SL ATR trop serré pour XAUUSD volatil
   - Signal valid MAIS timing mauvais (news non anticipée)
   - Loss guard a limité dégâts (sinon -$1500+)
   - Lesson: Vérifier calendrier économique avant burst
```

---

## 8. MODULES DE SUPPORT

### 8.1 ConfigManager (Singleton Central)

```
┌─────────────────────────────────────────────────────────────────────────┐
│ ConfigManager - Orchestration Centralisée Configuration                │
│ Fichier: core/config_manager.py                                        │
├─────────────────────────────────────────────────────────────────────────┤
│ RESPONSABILITÉS:                                                         │
│                                                                          │
│ 1. Chargement Configs Statiques                                         │
│    ├─ config/prod_config.json (config globale)                          │
│    ├─ config/broker_accounts.json (credentials MT5)                     │
│    ├─ config/assets/*.json (EURUSD, GBPUSD, XAUUSD)                     │
│    └─ config/strategy/*.json (scalping, liquidity)                      │
│                                                                          │
│ 2. Configuration Dynamique Runtime                                      │
│    ├─ Merge configs (prod + asset + strategy)                           │
│    ├─ Cache runtime (évite I/O répétés)                                 │
│    ├─ Hot-reload (watch file changes)                                   │
│    └─ Validation schémas JSON                                           │
│                                                                          │
│ 3. Access Hiérarchisé (Dot Notation)                                    │
│    config_manager.get("global_safety.max_open_positions")               │
│    → 15                                                                  │
│                                                                          │
│ 4. Gestion Credentials                                                  │
│    ├─ MT5: get_mt5_account_credentials(mode="DEMO"/"LIVE")              │
│    ├─ Telegram: get("env_vars.TELEGRAM_BOT_TOKEN")                      │
│    └─ Secrets masqués dans logs                                         │
│                                                                          │
│ 5. Alertes Telegram                                                     │
│    send_alert(message, channel="telegram_critical")                     │
│                                                                          │
│ 6. Summaries Périodiques                                                │
│    process_and_send_summary_alert(context={...})                        │
│    → Résumé compte toutes les 4h                                        │
│                                                                          │
│ PATTERN SINGLETON:                                                       │
│   _instance = None                                                      │
│   def __new__(cls):                                                     │
│       if cls._instance is None:                                         │
│           cls._instance = super().__new__(cls)                          │
│       return cls._instance                                              │
└─────────────────────────────────────────────────────────────────────────┘
```

### 8.2 AuditLogger (Trail Complet)

```
┌─────────────────────────────────────────────────────────────────────────┐
│ AuditLogger - Audit Trail Exhaustif                                    │
│ Fichier: core/audit_logger.py                                          │
├─────────────────────────────────────────────────────────────────────────┤
│ ÉVÉNEMENTS LOGGÉS:                                                       │
│                                                                          │
│ 1. Configuration Changes                                                │
│    log_config_change(change_info, source, snapshot)                     │
│    → Toute modification config (strategy reload, param update)          │
│                                                                          │
│ 2. Trade Executions                                                     │
│    log_trade_execution(trade_details)                                   │
│    → Ouverture position (ticket, entry, SL/TP, strategy)                │
│                                                                          │
│ 3. Trade Closures                                                       │
│    log_trade_closure(ticket, close_price, profit, reason)               │
│    → Fermeture position (profit/loss, raison)                           │
│                                                                          │
│ 4. Basket Operations                                                    │
│    log_basket_operation(basket_id, operation, details)                  │
│    → Création/fermeture baskets burst                                   │
│                                                                          │
│ 5. Errors & Exceptions                                                  │
│    log_error(error_type, message, traceback, context)                   │
│    → Toutes erreurs runtime (MT5, validation, calcul)                   │
│                                                                          │
│ 6. System Events                                                        │
│    log_system_event(event_type, details)                                │
│    → Démarrage/arrêt bot, connexion/déconnexion MT5                     │
│                                                                          │
│ FORMAT FICHIER:                                                          │
│   logs/audit_{YYYYMMDD}.log (JSON Lines)                                │
│   {"timestamp": "...", "event_type": "...", "data": {...}}              │
│                                                                          │
│ ROTATION:                                                                │
│   - Nouveau fichier chaque jour (minuit UTC)                            │
│   - Compression archives après 7 jours                                  │
│   - Rétention: 90 jours                                                 │
└─────────────────────────────────────────────────────────────────────────┘
```

### 8.3 Mecano (Diagnostics Système)

```
┌─────────────────────────────────────────────────────────────────────────┐
│ Mecano - Diagnostics & Health Checks                                   │
│ Fichier: mecanique_generale/mecano.py                                  │
├─────────────────────────────────────────────────────────────────────────┤
│ MÉTRIQUES SURVEILLÉES:                                                   │
│                                                                          │
│ 1. Ressources Système                                                   │
│    ├─ CPU usage (%) - Alert si > 80%                                    │
│    ├─ RAM usage (MB) - Alert si > 90% total                             │
│    ├─ Disk usage (%) - Alert si > 85%                                   │
│    └─ Process threads count                                             │
│                                                                          │
│ 2. Connexions                                                            │
│    ├─ MT5 connection status (ping)                                      │
│    ├─ Network latency (ms)                                              │
│    └─ Last successful data fetch timestamp                              │
│                                                                          │
│ 3. Performance Bot                                                       │
│    ├─ Cycle duration avg (secondes)                                     │
│    ├─ Cycles completed today                                            │
│    ├─ Errors count (last 24h)                                           │
│    └─ Memory leaks detection                                            │
│                                                                          │
│ 4. Trading Metrics                                                       │
│    ├─ Positions ouvertes count                                          │
│    ├─ Daily trades count                                                │
│    ├─ Daily P&L                                                         │
│    └─ Exposure risk current                                             │
│                                                                          │
│ ACTIONS:                                                                 │
│   - Health check toutes les 15 minutes                                  │
│   - Alerte Telegram si anomalie détectée                                │
│   - Auto-restart si deadlock détecté                                    │
│   - Logs détaillés: logs/mecano_{date}.log                              │
└─────────────────────────────────────────────────────────────────────────┘
```

---

## 9. GESTION DES ERREURS ET RÉCUPÉRATION

### 9.1 Hiérarchie des Erreurs

```
┌─────────────────────────────────────────────────────────────────────────┐
│ HIÉRARCHIE DES ERREURS - SNIPER_X                                      │
├─────────────────────────────────────────────────────────────────────────┤
│                                                                          │
│ NIVEAU 1: ERREURS CRITIQUES (Bot Stop)                                  │
│ ├─ MT5 Connection Lost (reconnect failed après 3 tentatives)            │
│ ├─ ConfigManager initialization failed                                  │
│ ├─ Corrupted critical config files                                      │
│ ├─ Account credentials invalid                                          │
│ └─ Insufficient permissions (file system, MT5)                          │
│    → ACTION: STOP BOT, Alerte Telegram CRITICAL, Exit code 1            │
│                                                                          │
│ NIVEAU 2: ERREURS GRAVES (Skip Cycle)                                   │
│ ├─ Market data fetch failed                                             │
│ ├─ Strategy load error                                                  │
│ ├─ Analysis module crash (OrderFlow, Footprint, VWAP)                   │
│ ├─ Validation checks failed (spread, exposure)                          │
│ └─ Order execution rejected (retcode != DONE)                           │
│    → ACTION: LOG ERROR, Skip cycle, Continue bot, Alerte si répété 3x   │
│                                                                          │
│ NIVEAU 3: ERREURS MINEURES (Log Warning)                                │
│ ├─ Ticks data incomplete (< 20 ticks)                                   │
│ ├─ Footprint status SUSPECT                                             │
│ ├─ Cache miss (performance hit, pas blocant)                            │
│ ├─ Telegram send failed (retry 1x)                                      │
│ └─ Reconciliation minor mismatch                                        │
│    → ACTION: LOG WARNING, Continue normalement                          │
│                                                                          │
│ NIVEAU 4: INFO (Non-errors)                                             │
│ ├─ No signal detected (HOLD decision)                                   │
│ ├─ Position closed normally                                             │
│ ├─ Config hot-reloaded successfully                                     │
│ └─ Health check passed                                                  │
│    → ACTION: LOG INFO                                                   │
└─────────────────────────────────────────────────────────────────────────┘
```

### 9.2 Stratégies de Récupération

```
┌─────────────────────────────────────────────────────────────────────────┐
│ STRATÉGIES DE RÉCUPÉRATION                                             │
├─────────────────────────────────────────────────────────────────────────┤
│                                                                          │
│ 1. MT5 Disconnection                                                    │
│    ┌───────────────────────────────────────────────────────────┐       │
│    │ DÉTECTION:                                                 │       │
│    │   - mt5.terminal_info() returns None                       │       │
│    │   - mt5.account_info() returns None                        │       │
│    │   - Order send retcode: CONNECTION_ERROR                   │       │
│    │                                                             │       │
│    │ RÉCUPÉRATION:                                              │       │
│    │   1. Log warning "MT5 connection lost"                     │       │
│    │   2. Wait 5 seconds                                        │       │
│    │   3. mt5.shutdown()                                        │       │
│    │   4. mt5.initialize()                                      │       │
│    │   5. mt5.login(account_details)                            │       │
│    │   6. Vérifier account_info                                 │       │
│    │   7. Réconciliation positions                              │       │
│    │   8. If success: Resume bot                                │       │
│    │   9. If failed after 3 attempts: CRITICAL STOP             │       │
│    └───────────────────────────────────────────────────────────┘       │
│                                                                          │
│ 2. Market Data Fetch Failed                                             │
│    ┌───────────────────────────────────────────────────────────┐       │
│    │ DÉTECTION:                                                 │       │
│    │   - mt5.copy_rates_from_pos() returns None                 │       │
│    │   - DataFrame empty ou < 10 bougies                        │       │
│    │                                                             │       │
│    │ RÉCUPÉRATION:                                              │       │
│    │   1. Log error avec asset + timeframe                      │       │
│    │   2. Retry fetch 1x (délai 2s)                             │       │
│    │   3. If still failed: Skip asset ce cycle                  │       │
│    │   4. Use cached data si disponible (< 5 min)               │       │
│    │   5. If repeated 3 cycles: Alert Telegram                  │       │
│    └───────────────────────────────────────────────────────────┘       │
│                                                                          │
│ 3. Analysis Module Crash                                                │
│    ┌───────────────────────────────────────────────────────────┐       │
│    │ DÉTECTION:                                                 │       │
│    │   - Exception in OrderFlowV6.analyze()                     │       │
│    │   - Exception in FootprintAnalyzer.analyze()               │       │
│    │   - Exception in VWAPAnalyzer.analyze()                    │       │
│    │                                                             │       │
│    │ RÉCUPÉRATION:                                              │       │
│    │   1. Catch exception, log full traceback                   │       │
│    │   2. Return degraded result:                               │       │
│    │      {status: "INVALID", score: 0, error: "..."}           │       │
│    │   3. FusionManager detects missing component               │       │
│    │   4. Falls back to available components                    │       │
│    │   5. If all 3 modules fail: Skip cycle entirely            │       │
│    │   6. Alert if crash persists 5+ cycles                     │       │
│    └───────────────────────────────────────────────────────────┘       │
│                                                                          │
│ 4. Order Execution Rejected                                             │
│    ┌───────────────────────────────────────────────────────────┐       │
│    │ DÉTECTION:                                                 │       │
│    │   - result.retcode != TRADE_RETCODE_DONE                   │       │
│    │                                                             │       │
│    │ RÉCUPÉRATION (selon retcode):                              │       │
│    │   ├─ REQUOTE (10004):                                      │       │
│    │   │    → Retry 1x avec nouveau prix (si dans deviation)    │       │
│    │   │                                                         │       │
│    │   ├─ INVALID_VOLUME (10013):                               │       │
│    │   │    → Ajuster volume au pas lot correct, retry 1x       │       │
│    │   │                                                         │       │
│    │   ├─ INVALID_STOPS (10015):                                │       │
│    │   │    → Recalculer SL/TP avec stops_level min, retry 1x   │       │
│    │   │                                                         │       │
│    │   ├─ MARKET_CLOSED (10018):                                │       │
│    │   │    → Skip trade, log warning, continue                 │       │
│    │   │                                                         │       │
│    │   ├─ NO_MONEY (10019):                                     │       │
│    │   │    → Reduce volume 50%, retry 1x                       │       │
│    │   │    → If still fail: Alert CRITICAL (marge insuffisante)│       │
│    │   │                                                         │       │
│    │   └─ AUTRES:                                               │       │
│    │        → Log error, skip trade, alert si répété            │       │
│    └───────────────────────────────────────────────────────────┘       │
│                                                                          │
│ 5. Config File Corruption                                               │
│    ┌───────────────────────────────────────────────────────────┐       │
│    │ DÉTECTION:                                                 │       │
│    │   - JSON parse error                                       │       │
│    │   - Schema validation failed                               │       │
│    │                                                             │       │
│    │ RÉCUPÉRATION:                                              │       │
│    │   1. Log critical error avec path fichier                  │       │
│    │   2. Try load backup file (.bak)                           │       │
│    │   3. If backup OK: Use backup, alert user                  │       │
│    │   4. If no backup: Use hardcoded defaults (safe mode)      │       │
│    │   5. Alert Telegram CRITICAL                               │       │
│    │   6. Create incident report                                │       │
│    └───────────────────────────────────────────────────────────┘       │
└─────────────────────────────────────────────────────────────────────────┘
```

---

## 10. LOGS ET AUDIT TRAIL

### 10.1 Structure des Logs

```
┌─────────────────────────────────────────────────────────────────────────┐
│ STRUCTURE COMPLÈTE DES LOGS                                            │
├─────────────────────────────────────────────────────────────────────────┤
│                                                                          │
│ logs/                                                                    │
│ ├── sniper_x_20251206.log          # Log principal (tout)               │
│ │   Format: [TIMESTAMP] [MODULE] [LEVEL] Message                        │
│ │   Rotation: Daily (minuit UTC)                                        │
│ │   Rétention: 30 jours                                                 │
│ │   Exemple:                                                            │
│ │   2025-12-06 14:32:00 [main] INFO SNIPER_X Bot démarré en mode DEMO   │
│ │   2025-12-06 14:32:03 [OrderFlowV6] DEBUG Score: 78, Bias: BUY        │
│ │                                                                        │
│ ├── trades_20251206.log             # Trades uniquement                 │
│ │   Format: [TIME] | [ASSET] | [ACTION] | [DETAILS]                     │
│ │   Rotation: Daily                                                     │
│ │   Rétention: 90 jours                                                 │
│ │   Exemple:                                                            │
│ │   14:32:18 | XAUUSD | BUY | Ticket:123456781 | Entry:2050.51 | ...    │
│ │   14:47:09 | XAUUSD | CLOSE_BASKET | Profit:+$118.40 | Duration:15min │
│ │                                                                        │
│ ├── audit_20251206.log              # Audit trail (JSON Lines)          │
│ │   Format: 1 JSON object par ligne                                     │
│ │   Rotation: Daily                                                     │
│ │   Rétention: 365 jours (compliance)                                   │
│ │   Exemple:                                                            │
│ │   {"timestamp":"2025-12-06T14:32:18Z","event":"TRADE_EXECUTION",...}  │
│ │   {"timestamp":"2025-12-06T14:47:09Z","event":"BASKET_CLOSED",...}    │
│ │                                                                        │
│ ├── mecano_20251206.log             # Diagnostics système               │
│ │   Format: [TIME] [CHECK] [STATUS] Details                             │
│ │   Rotation: Daily                                                     │
│ │   Rétention: 7 jours                                                  │
│ │   Exemple:                                                            │
│ │   14:30:00 HEALTH_CHECK OK CPU:45% RAM:2.1GB Disk:62%                 │
│ │   14:45:00 HEALTH_CHECK WARN CPU:82% (threshold exceeded)             │
│ │                                                                        │
│ ├── errors_20251206.log             # Erreurs uniquement                │
│ │   Format: [TIME] [ERROR_TYPE] [SEVERITY] Message + Traceback          │
│ │   Rotation: Daily                                                     │
│ │   Rétention: 90 jours                                                 │
│ │   Exemple:                                                            │
│ │   14:35:22 MT5_ERROR CRITICAL Connection lost - attempting reconnect  │
│ │   Traceback: ...                                                      │
│ │                                                                        │
│ └── strategy_manager_debug.log     # Debug stratégies (temp)            │
│     Format: [TIME] [STRATEGY_KEY] Message                               │
│     Rotation: Manuel (dev only)                                         │
│     Exemple:                                                            │
│     [2025-12-06T14:32:00Z] [scalping] Module 'scalping.py' exécuté OK   │
└─────────────────────────────────────────────────────────────────────────┘
```

### 10.2 Exemple Audit Trail Complet (JSON)

```json
{
  "timestamp": "2025-12-06T14:32:18.423Z",
  "event_type": "TRADE_EXECUTION",
  "asset": "XAUUSD",
  "action": "BUY",
  "ticket": 123456781,
  "entry_price": 2050.51,
  "volume": 0.98,
  "sl": 2050.3725,
  "tp": null,
  "strategy": "scalping",
  "magic": 52001,
  "basket_id": "BURST_XAUUSD_20251206143212_A7F2",
  "confidence": 0.82,
  "signal_type": "HIGH_CONVICTION_BUY",
  "context": {
    "cycle": 42,
    "market_phase": "TRENDING_BULLISH",
    "vwap_regime": "TRENDING",
    "orderflow_score": 78,
    "footprint_score": 65,
    "vwap_score": 0.82,
    "fusion_weights": {
      "orderflow": 0.30,
      "footprint": 0.20,
      "vwap": 0.50
    },
    "coherence": {
      "majority": "BUY",
      "agreement": 0.95,
      "aligned_components": 3
    }
  },
  "execution_details": {
    "retcode": 10009,
    "comment": "Done",
    "slippage_pips": 0.01,
    "spread_at_execution": 1.2,
    "execution_time_ms": 87
  },
  "risk_metrics": {
    "risk_amount_usd": 100.0,
    "risk_percent": 1.0,
    "sl_distance_pips": 12.75,
    "potential_loss_usd": -99.75,
    "account_equity_before": 10000.0
  },
  "metadata": {
    "bot_version": "4.4-unblocked",
    "mode": "DEMO",
    "mt5_account": 12345678,
    "server": "Broker-Demo",
    "user_id": "admin"
  }
}
```

---

## CONCLUSION

Ce document décrit **l'intégralité du pipeline SNIPER_X**, depuis le démarrage du bot jusqu'à la fermeture d'un trade avec audit complet.

### Points Clés à Retenir

1. **Pipeline en 7 Étapes**: Data → Analysis → Fusion → Decision → Validation → Execution → Monitoring
2. **3 Modules d'Analyse**: OrderFlow V6 (score 0-100) + Footprint M1 + VWAP (score 0-1)
3. **Fusion Adaptative**: Poids dynamiques selon régime VWAP (TRENDING: 50% VWAP)
4. **Burst Scalping**: 8 tickets parallèles, SL commun ATR, fermeture +15 pips
5. **Sécurité Multi-Niveaux**: 5 validators pré-exécution + reconciliation post-trade
6. **Audit Exhaustif**: Logs JSON + texte, rétention 90-365 jours

### Fichiers Critiques

- `main.py`: Point d'entrée (démarrage)
- `run_bot.py`: Boucle principale (cycle 60s)
- `decision_pipeline.py`: Orchestration décisions
- `fusion_manager.py`: Fusion signaux (score final)
- `trade_executor.py`: Exécution + monitoring
- `config_manager.py`: Configuration centralisée

### Prochaines Évolutions Possibles

- Dashboard Streamlit temps réel (positions, P&L, signaux)
- Backtesting moteur (replay historical ticks)
- Machine Learning pour poids adaptatifs (optimisation continue)
- Multi-broker support (IBKR, OANDA)

---

**Document Version**: 2.0 (Partie 2 complète)
**Dernière Mise à Jour**: 6 Décembre 2025
**Auteur**: Architecture Team SNIPER_X
