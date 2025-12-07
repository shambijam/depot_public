# DIAGRAMME PIPELINE SNIPER_X - Mermaid
## Visualisation Interactive du Pipeline de Trading

**Version:** 1.0
**Date:** 6 Décembre 2025

---

## INSTRUCTIONS D'UTILISATION

Ce document contient des diagrammes **Mermaid** qui peuvent être visualisés de plusieurs façons:

1. **GitHub/GitLab:** Rendu automatique dans l'interface web
2. **VS Code:** Extension "Markdown Preview Mermaid Support"
3. **Mermaid Live Editor:** https://mermaid.live (copier-coller le code)
4. **Obsidian:** Rendu natif dans les notes markdown

---

## 1. PIPELINE COMPLET (VUE GLOBALE)

```mermaid
graph TB
    Start([Bot Démarrage<br/>main.py]) --> Init[PHASE INITIALISATION<br/>ConfigManager + MT5Connector<br/>StrategyManager + DecisionPipeline]

    Init --> Verify{Vérifications<br/>Pré-démarrage}
    Verify -->|❌ Échec| Stop([STOP BOT<br/>Alert Telegram])
    Verify -->|✅ OK| Loop[Alignement Horloge Suisse<br/>sleep_until_next_minute]

    Loop --> CycleStart[CYCLE N démarré<br/>Timestamp exact]

    CycleStart --> DataCollect[A. MARKET DATA COLLECTION<br/>MT5: OHLC M1/M5/M15 + Ticks]

    DataCollect --> SignalGen[B. SIGNAL GENERATION<br/>OrderFlow V6 + Footprint + VWAP]

    SignalGen --> Fusion[C. FUSION MANAGER<br/>Poids adaptatifs selon VWAP regime]

    Fusion --> Decision[D. DECISION PIPELINE<br/>Sélection stratégie + Validation]

    Decision --> ValidCheck{E. VALIDATORS<br/>5 checks}
    ValidCheck -->|❌ Rejet| LogSkip[Log raison skip]
    ValidCheck -->|✅ Pass| OrderBuild[F. ORDER BUILDER<br/>Sizing + SL/TP]

    OrderBuild --> ExecCheck{Burst?}
    ExecCheck -->|Oui| BurstExec[G. BURST HANDLER<br/>8 tickets parallèles]
    ExecCheck -->|Non| SingleExec[G. SINGLE EXECUTION<br/>1 ticket]

    BurstExec --> MT5Send[H. MT5 ORDER SEND<br/>mt5.order_send]
    SingleExec --> MT5Send

    MT5Send --> ExecResult{Retcode?}
    ExecResult -->|10009 DONE| Success[✅ Position Ouverte<br/>Cache + Audit + Telegram]
    ExecResult -->|10004 REQUOTE| Retry[Retry 1x<br/>nouveau prix]
    ExecResult -->|Autre| Failed[❌ Échec<br/>Log + Skip]

    Retry --> MT5Send

    Success --> Monitor[I. MONITORING<br/>Burst basket surveillance]
    Failed --> LogSkip
    LogSkip --> NextCycle

    Monitor --> ProfitCheck{Profit target<br/>atteint?}
    ProfitCheck -->|Oui +15 pips| CloseProfit[J. CLOSE BASKET<br/>Profit realized]
    ProfitCheck -->|Non| LossCheck{Loss guard<br/>triggered?}

    LossCheck -->|Oui -110 pips| CloseEmergency[J. EMERGENCY CLOSE<br/>Loss limited]
    LossCheck -->|Non| NextCycle[K. SLEEP UNTIL NEXT MINUTE<br/>60s précis]

    CloseProfit --> Reconcile[L. RECONCILIATION<br/>MT5 positions sync]
    CloseEmergency --> Reconcile

    Reconcile --> AuditLog[M. AUDIT LOGGING<br/>JSON trail complet]

    AuditLog --> NextCycle
    NextCycle --> CycleStart

    style Start fill:#4CAF50,stroke:#2E7D32,color:#fff
    style Stop fill:#F44336,stroke:#C62828,color:#fff
    style Success fill:#4CAF50,stroke:#2E7D32,color:#fff
    style Failed fill:#F44336,stroke:#C62828,color:#fff
    style CloseProfit fill:#4CAF50,stroke:#2E7D32,color:#fff
    style CloseEmergency fill:#FF9800,stroke:#E65100,color:#fff
    style Fusion fill:#2196F3,stroke:#1565C0,color:#fff
```

---

## 2. SIGNAL GENERATION (DÉTAIL MODULE B)

```mermaid
graph LR
    subgraph MarketData[Market Data Input]
        DF_M1[DataFrame M1<br/>120 bougies]
        DF_M5[DataFrame M5<br/>240 bougies]
        DF_M15[DataFrame M15<br/>240 bougies]
        Ticks[Ticks M1<br/>387 ticks avg]
    end

    subgraph Analysis[Analyse Parallèle]
        DF_M1 --> OF[OrderFlow V6<br/>Volume Profile]
        DF_M5 --> OF
        Ticks --> OF

        Ticks --> FP[Footprint M1<br/>Delta Analysis]

        DF_M1 --> VWAP[VWAP Analyzer<br/>Regime Detection]
        DF_M5 --> VWAP
    end

    subgraph Results[Résultats Signaux]
        OF --> OF_OUT[Score: 78/100<br/>Bias: BUY<br/>Status: VALID]
        FP --> FP_OUT[Score: 65/100<br/>Delta: +4040<br/>Status: VALID]
        VWAP --> VWAP_OUT[Score: 0.82/1.0<br/>Regime: TRENDING<br/>Bias: BULLISH]
    end

    OF_OUT --> Fusion[FusionManager]
    FP_OUT --> Fusion
    VWAP_OUT --> Fusion

    Fusion --> Final[Fused Confidence: 0.82<br/>Direction: BUY<br/>Signal: HIGH_CONVICTION]

    style OF fill:#9C27B0,stroke:#6A1B9A,color:#fff
    style FP fill:#FF9800,stroke:#E65100,color:#fff
    style VWAP fill:#2196F3,stroke:#1565C0,color:#fff
    style Fusion fill:#4CAF50,stroke:#2E7D32,color:#fff
    style Final fill:#4CAF50,stroke:#2E7D32,color:#fff
```

---

## 3. FUSION MANAGER (POIDS ADAPTATIFS)

```mermaid
graph TD
    Input[Signaux Input:<br/>OrderFlow + Footprint + VWAP]

    Input --> RegimeCheck{VWAP<br/>Regime?}

    RegimeCheck -->|TRENDING| W1[Poids TRENDING<br/>VWAP: 50%<br/>OrderFlow: 30%<br/>Footprint: 20%]
    RegimeCheck -->|BALANCED| W2[Poids BALANCED<br/>VWAP: 30%<br/>OrderFlow: 35%<br/>Footprint: 35%]
    RegimeCheck -->|ACCUMULATION| W3[Poids ACCUMULATION<br/>VWAP: 25%<br/>OrderFlow: 35%<br/>Footprint: 40%]
    RegimeCheck -->|TRANSITIONAL| W4[Poids TRANSITIONAL<br/>VWAP: 20%<br/>OrderFlow: 40%<br/>Footprint: 40%]

    W1 --> Calc[Calcul Score Pondéré]
    W2 --> Calc
    W3 --> Calc
    W4 --> Calc

    Calc --> Formula["weighted_score = <br/>(score_OF × w_OF) + <br/>(score_FP × w_FP) + <br/>(score_VWAP × w_VWAP)"]

    Formula --> Coherence{Cohérence<br/>3/3 alignés?}

    Coherence -->|Oui| Bonus[Bonus +0.05]
    Coherence -->|Non| ConflictCheck{Conflits?}

    ConflictCheck -->|2+| Malus[Malus ×0.85]
    ConflictCheck -->|1| MalusMini[Malus ×0.92]
    ConflictCheck -->|0| NoMalus[Pas de malus]

    Bonus --> FinalScore[Score Final: 0.0-0.99]
    Malus --> FinalScore
    MalusMini --> FinalScore
    NoMalus --> FinalScore

    FinalScore --> Threshold{Seuils}

    Threshold -->|≥ 0.60| High[HIGH_CONVICTION<br/>Exécution prioritaire]
    Threshold -->|≥ 0.55| Moderate[MODERATE<br/>Exécution normale]
    Threshold -->|≥ 0.50| Cautious[CAUTIOUS<br/>Prudent]
    Threshold -->|< 0.50| Hold[HOLD<br/>Attente]

    style W1 fill:#2196F3,stroke:#1565C0,color:#fff
    style W2 fill:#4CAF50,stroke:#2E7D32,color:#fff
    style W3 fill:#FF9800,stroke:#E65100,color:#fff
    style W4 fill:#F44336,stroke:#C62828,color:#fff
    style High fill:#4CAF50,stroke:#2E7D32,color:#fff
    style Moderate fill:#8BC34A,stroke:#558B2F,color:#fff
    style Cautious fill:#FFC107,stroke:#F57C00,color:#000
    style Hold fill:#9E9E9E,stroke:#616161,color:#fff
```

---

## 4. BURST EXECUTION (8 TICKETS PARALLÈLES)

```mermaid
sequenceDiagram
    participant DP as DecisionPipeline
    participant TE as TradeExecutor
    participant OB as OrderBuilder
    participant BH as BurstHandler
    participant MT5 as MT5Connector
    participant Broker as Broker MT5

    DP->>TE: execute_decision(decision)

    TE->>TE: Validators (5 checks)
    Note over TE: Spread, Exposure, Hours, Fat Finger, Manual

    TE->>OB: build_order_request(decision)
    OB->>OB: Sizing: equity × risk% / SL_distance
    OB->>OB: SLTP: ATR 1.5x, TP=None
    OB-->>TE: order_request (volume: 7.84 lots)

    TE->>BH: open_burst_basket(order_request, burst_size=8)

    BH->>BH: Division volume: 7.84/8 = 0.98 lots/ticket
    BH->>BH: Génération basket_id: BURST_XAUUSD_20251206143212_A7F2

    loop 8 tickets (i=1 to 8)
        BH->>MT5: order_send(ticket_i, volume=0.98)
        MT5->>Broker: TRADE_ACTION_DEAL (BUY 0.98 XAUUSD)
        Broker-->>MT5: retcode: 10009 DONE, ticket: 123456781
        MT5-->>BH: result (ticket, entry_price, status)

        alt Success
            BH->>BH: tickets.append(ticket_i)
            Note over BH: Ticket i/8 OK
        else Failed
            BH->>BH: Log error, continue next
            Note over BH: Ticket i/8 FAILED
        end
    end

    BH->>BH: Enregistrement basket (8 tickets, entry_avg: 2050.51)
    BH->>TE: basket_result (success: true, tickets_opened: 8/8)

    TE->>TE: Cache interne update
    TE->>TE: Audit logging (JSON trail)
    TE-->>DP: Execution SUCCESS

    Note over BH,MT5: Monitoring actif cycles suivants

    loop Cycles monitoring
        BH->>MT5: get_positions()
        MT5-->>BH: positions (8 tickets, profit_total)

        alt Profit target +15 pips
            BH->>BH: DÉCISION: Close basket
            BH->>MT5: close_basket (8 tickets)
            MT5->>Broker: CLOSE positions
            Broker-->>MT5: Confirmations (8×)
            BH->>TE: Basket closed (profit: +$118.40)
        else Loss guard -110 pips
            BH->>BH: DÉCISION: Emergency close
            BH->>MT5: close_basket IMMEDIATE
            MT5->>Broker: CLOSE positions
            Broker-->>MT5: Confirmations (8×)
            BH->>TE: Basket closed (loss: -$862)
        else Continue monitoring
            Note over BH: Holding positions
        end
    end
```

---

## 5. GESTION ERREURS (RÉCUPÉRATION)

```mermaid
stateDiagram-v2
    [*] --> Running: Bot démarré

    Running --> DataFetch: Cycle démarre

    DataFetch --> DataSuccess: MT5 data OK
    DataFetch --> DataFailed: MT5 data FAILED

    DataFailed --> RetryData: Retry 1x (2s)
    RetryData --> DataSuccess: Retry OK
    RetryData --> SkipCycle: Retry failed

    DataSuccess --> Analysis: Analyse signaux

    Analysis --> AnalysisSuccess: 3 modules OK
    Analysis --> AnalysisDegraded: 1-2 modules crash
    Analysis --> AnalysisFailed: 3 modules crash

    AnalysisDegraded --> Fusion: Fallback composants disponibles
    AnalysisFailed --> SkipCycle: Skip cycle

    AnalysisSuccess --> Fusion: Fusion signaux

    Fusion --> Decision: Score calculé

    Decision --> Execution: Action BUY/SELL
    Decision --> SkipCycle: Action HOLD

    Execution --> OrderSend: MT5 order_send

    OrderSend --> ExecSuccess: retcode 10009
    OrderSend --> ExecRequote: retcode 10004
    OrderSend --> ExecFailed: Autres retcodes

    ExecRequote --> RetryOrder: Retry 1x nouveau prix
    RetryOrder --> ExecSuccess: Retry OK
    RetryOrder --> ExecFailed: Retry failed

    ExecSuccess --> Monitoring: Position ouverte
    ExecFailed --> SkipCycle: Log erreur

    Monitoring --> Running: Cycle suivant

    SkipCycle --> Running: Cycle suivant

    Running --> MT5Disconnect: Connexion perdue
    MT5Disconnect --> Reconnect: Tentative reconnect

    Reconnect --> Running: Reconnect OK (3 max)
    Reconnect --> [*]: Reconnect failed après 3×

    note right of DataFailed
        Niveau 2: GRAVE
        → Skip cycle, continue bot
    end note

    note right of AnalysisDegraded
        Niveau 3: MINEUR
        → Warning log, continue
    end note

    note right of MT5Disconnect
        Niveau 1: CRITICAL
        → Reconnect ou stop bot
    end note
```

---

## 6. CYCLE DE VIE TRADE (EXEMPLE XAUUSD)

```mermaid
gantt
    title Trade XAUUSD Scalping Burst - Cycle de vie complet
    dateFormat  HH:mm:ss
    axisFormat %H:%M:%S

    section Détection
    Signal generation       :done, s1, 14:32:00, 6s
    Fusion signaux          :done, s2, after s1, 3s

    section Décision
    Validation pré-exec     :done, d1, after s2, 1s
    Construction ordre      :done, d2, after d1, 2s

    section Exécution
    Burst tickets 1-8       :done, e1, 14:32:13, 6s
    Confirmation MT5        :done, e2, after e1, 1s
    Telegram alert          :done, e3, after e2, 1s

    section Monitoring
    Cycle 43 (prix +17)     :active, m1, 14:33:00, 1m
    Cycle 44 (prix +31)     :active, m2, after m1, 1m
    Cycle 45 (prix +44)     :active, m3, after m2, 1m
    Cycle 46 (prix +61)     :active, m4, after m3, 1m
    Cycles 47-56            :active, m5, after m4, 10m
    Cycle 57 (prix +15)     :crit, m6, after m5, 1m

    section Fermeture
    Profit target atteint   :done, c1, 14:47:01, 1s
    Close basket 8 tickets  :done, c2, after c1, 6s
    Reconciliation          :done, c3, after c2, 2s
    Audit logging           :done, c4, after c3, 2s

    section Bilan
    Trade finalisé          :milestone, 14:47:11, 0s
```

---

## 7. ARCHITECTURE MODULES (DÉPENDANCES)

```mermaid
graph TB
    subgraph Core[CORE LAYER]
        CM[ConfigManager<br/>Singleton]
        SM[StrategyManager]
        DE[DataEngine]
    end

    subgraph Intelligence[INTELLIGENCE LAYER]
        MA[MarketAnalyzer]
        OF[OrderFlow V6]
        FP[Footprint M1]
        VW[VWAP Analyzer]
        FM[FusionManager]
        DP[DecisionPipeline]
        PO[PhaseObserver]
    end

    subgraph Execution[EXECUTION LAYER]
        TE[TradeExecutor]
        OB[OrderBuilder]
        SZ[Sizing Module]
        SLTP[SLTP Module]
        BH[BurstHandler]
        MT5[MT5Connector<br/>Singleton]
        AL[AuditLogger]
    end

    CM --> SM
    CM --> DP
    CM --> TE

    SM --> DP
    DE --> MA

    MA --> OF
    MA --> FP
    MA --> VW
    MA --> FM
    MA --> PO

    OF --> FM
    FP --> FM
    VW --> FM

    FM --> DP

    DP --> TE

    TE --> OB
    TE --> BH
    TE --> MT5
    TE --> AL

    OB --> SZ
    OB --> SLTP

    BH --> MT5

    style CM fill:#FF6B6B,stroke:#C92A2A,color:#fff
    style SM fill:#FF6B6B,stroke:#C92A2A,color:#fff
    style DE fill:#FF6B6B,stroke:#C92A2A,color:#fff

    style MA fill:#4ECDC4,stroke:#0B7285,color:#fff
    style FM fill:#4ECDC4,stroke:#0B7285,color:#fff
    style DP fill:#4ECDC4,stroke:#0B7285,color:#fff

    style TE fill:#95E1D3,stroke:#087F5B,color:#000
    style MT5 fill:#95E1D3,stroke:#087F5B,color:#000
    style AL fill:#95E1D3,stroke:#087F5B,color:#000
```

---

## 8. LOGS & AUDIT FLOW

```mermaid
flowchart LR
    subgraph Events[Événements Système]
        E1[Trade Execution]
        E2[Basket Closure]
        E3[Config Change]
        E4[Error Occurred]
        E5[System Event]
    end

    subgraph Loggers[Logging Modules]
        E1 --> TL[TradeLogger]
        E2 --> TL
        E1 --> AL[AuditLogger]
        E2 --> AL
        E3 --> AL
        E4 --> AL
        E5 --> AL
        E4 --> EL[ErrorLogger]
    end

    subgraph Files[Fichiers Logs]
        TL --> F1[(trades_{date}.log<br/>Texte lisible<br/>Retention: 90j)]
        AL --> F2[(audit_{date}.log<br/>JSON Lines<br/>Retention: 365j)]
        EL --> F3[(errors_{date}.log<br/>Traceback full<br/>Retention: 90j)]
        AL --> F4[(audit trail database<br/>Optionnel SQL)]
    end

    subgraph Alerts[Alertes]
        E4 --> T1[Telegram Critical<br/>Erreurs niveau 1]
        E1 --> T2[Telegram Trades<br/>Ouverture/Fermeture]
        E5 --> T3[Telegram Health<br/>Checks 4h]
    end

    style F1 fill:#FFE66D,stroke:#F59F00,color:#000
    style F2 fill:#4ECDC4,stroke:#0B7285,color:#fff
    style F3 fill:#FF6B6B,stroke:#C92A2A,color:#fff
    style F4 fill:#A8DADC,stroke:#1864AB,color:#000

    style T1 fill:#FF6B6B,stroke:#C92A2A,color:#fff
    style T2 fill:#4ECDC4,stroke:#0B7285,color:#fff
    style T3 fill:#95E1D3,stroke:#087F5B,color:#000
```

---

## NOTES D'UTILISATION

### Visualisation Recommandée
1. **Diagramme 1** (Pipeline complet) - Vue globale du flux
2. **Diagramme 4** (Burst execution) - Séquence détaillée burst
3. **Diagramme 3** (Fusion Manager) - Logique scoring adaptatif
4. **Diagramme 5** (Gestion erreurs) - États et récupération

### Légende Couleurs
- 🟢 **Vert** → Succès, état actif
- 🔴 **Rouge** → Erreur, état critique
- 🔵 **Bleu** → Processus normal, intelligence
- 🟠 **Orange** → Warning, état dégradé
- ⚫ **Gris** → Neutre, attente

### Export Formats
Les diagrammes Mermaid peuvent être exportés en:
- **PNG/SVG** (via mermaid.live)
- **PDF** (via pandoc + mermaid-filter)
- **HTML interactif** (via mermaid.js)

---

**Document Version:** 1.0
**Dernière MAJ:** 6 Décembre 2025
**Auteur:** Architecture Team SNIPER_X
**Référence:** ARBRE_GENEALOGIQUE_PROCESSUS.md + RESUME_EXECUTIF.md
