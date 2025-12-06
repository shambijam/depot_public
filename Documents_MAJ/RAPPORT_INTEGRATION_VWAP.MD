🏦 MODULE VWAP INSTITUTIONNEL - SPECIFICATIONS TECHNIQUES COMPLÈTES
📋 DOCUMENT DE SPÉCIFICATIONS TECHNIQUES
Référence : SNIPER-X-VWAP-MODULE-v1.0.0
Date : [DATE]
Statut : DRAFT
Classification : INTERNE - HAUTE SENSIBILITÉ
1. 🎯 OBJECTIFS FONCTIONNELS
1.1 Objectif Principal
Fournir un module de calcul et d'analyse VWAP de niveau institutionnel, intégré nativement dans le pipeline de décision SNIPER-X, avec une latence inférieure à 1ms pour les lectures et une précision de 0.00001 pip.

1.2 Exigences Fonctionnelles
text
EF-001 : Calcul VWAP en temps réel avec reset quotidien configurable
EF-002 : Support multi-timeframe (M1, M5, M15, H1)
EF-003 : Dérivés VWAP (pente, courbure, bandes, écarts)
EF-004 : Intégration seamless avec l'architecture multi-threads existante
EF-005 : Cache L1/L2 pour performances nanosecondes
EF-006 : Métriques temps réel avec export Prometheus/Grafana
EF-007 : Health checks et auto-réparation
EF-008 : Audit trail complet (traceabilité des calculs)
EF-009 : Backtesting intégré avec replay historique
EF-010 : API REST pour monitoring externe
2. 🏗️ ARCHITECTURE TECHNIQUE
2.1 Vue d'ensemble du système
text
┌─────────────────────────────────────────────────────────────────┐
│                     VWAP MODULE ARCHITECTURE                    │
├─────────────────────────────────────────────────────────────────┤
│  LAYER 0 : DATA SOURCES                                         │
│  ├── MT5 Ticks Stream (Real-time)                              │
│  ├── MT5 Rates (M1, M5, etc.)                                  │
│  └── Historical Data (CSV, Parquet, Database)                  │
│                                                                 │
│  LAYER 1 : INGESTION & VALIDATION                               │
│  ├── Tick Validator (spread, outliers, holes)                  │
│  ├── Rate Resampler (to any timeframe)                         │
│  └── Data Quality Monitor (real-time alerts)                   │
│                                                                 │
│  LAYER 2 : COMPUTATION ENGINE                                   │
│  ├── VWAP Core Calculator (Cython optimized)                   │
│  ├── VWAP Derivatives Calculator                                │
│  ├── Statistical Analysis Engine                                │
│  └── Anomaly Detection System                                   │
│                                                                 │
│  LAYER 3 : CACHING & STORAGE                                    │
│  ├── L1 Cache (in-memory, <1ms)                                │
│  ├── L2 Cache (Redis, <5ms)                                    │
│  ├── L3 Storage (PostgreSQL TimescaleDB)                       │
│  └── Archive (Parquet + S3/GCS)                                │
│                                                                 │
│  LAYER 4 : API & INTEGRATION                                    │
│  ├── Python API (sync/async)                                   │
│  ├── WebSocket Streaming                                        │
│  ├── REST API (FastAPI)                                        │
│  └── Integration Hooks (existing pipeline)                     │
│                                                                 │
│  LAYER 5 : MONITORING & OBSERVABILITY                           │
│  ├── Metrics (Prometheus)                                      │
│  ├── Logging (structured JSON)                                 │
│  ├── Tracing (OpenTelemetry)                                   │
│  └── Alerting (PagerDuty/Teams/Slack)                          │
└─────────────────────────────────────────────────────────────────┘
2.2 Intégration dans l'architecture existante
text
┌─────────────────────────────────────────────────────────────────┐
│         INTEGRATION WITH SNIPER-X PIPELINE                      │
├─────────────────────────────────────────────────────────────────┤
│  BEFORE (current):                                              │
│  Data Engine (5s) → Footprint Cache → Scalping Thread (10s)    │
│                                                                 │
│  AFTER (with VWAP):                                             │
│  Data Engine (5s) → Footprint Cache → VWAP Cache → Scalping    │
│                ↗                                               │
│  VWAP Engine (1s) ────────────────────────────────────────┘    │
│                                                                 │
│  COMMUNICATION PROTOCOL:                                        │
│  • ZeroMQ PUB/SUB pour streaming temps réel                    │
│  • Shared memory pour ultra-basse latence                      │
│  • Redis pour cross-process communication                      │
└─────────────────────────────────────────────────────────────────┘
2.3 Topologie des threads
python
# Nouvelle architecture multi-threads avec VWAP
Threads = {
    "main": {"cycle": None, "priority": "normal"},
    "data_engine": {"cycle": "5s", "priority": "high"},
    "vwap_engine": {"cycle": "1s", "priority": "realtime"},
    "scalping_thread": {"cycle": "10s", "priority": "high"},
    "liquidity_thread": {"cycle": "60s", "priority": "normal"},
    "basket_monitor": {"cycle": "100ms", "priority": "realtime"},
    "vwap_derivatives": {"cycle": "10s", "priority": "low"},
    "health_monitor": {"cycle": "30s", "priority": "background"},
}
3. ⚙️ SPÉCIFICATIONS DÉTAILLÉES DU MODULE
3.1 Module VWAP Core
python
"""
Module: vwap_core.py
Dépendances: numpy, numba, cython, pandas
Performance: < 0.1ms par calcul
"""

class VWAPCalculator:
    """Calculateur VWAP optimisé niveau institutionnel"""
    
    def __init__(self, config: dict):
        # Configuration
        self.symbol: str = config['symbol']
        self.reset_hour: int = config.get('reset_hour', 0)  # GMT
        self.reset_timezone: str = config.get('reset_timezone', 'GMT')
        self.pip_value: float = config.get('pip_value', 0.01)  # XAUUSD
        self.decimal_places: int = config.get('decimal_places', 2)
        
        # État
        self.cumulative_tpv: float = 0.0  # Cumulative Typical Price * Volume
        self.cumulative_volume: float = 0.0
        self.current_vwap: float = 0.0
        self.last_reset: datetime = None
        self.session_start_price: float = None
        
        # Historique (ring buffer)
        self.history_size: int = config.get('history_size', 86400)  # 24h en secondes
        self.vwap_history = np.zeros(self.history_size, dtype=np.float64)
        self.time_history = np.zeros(self.history_size, dtype=np.datetime64)
        self.pointer: int = 0
        
        # Cache
        self.cache = {
            'current': {'value': 0.0, 'timestamp': None},
            'derivatives': {},
            'signals': {}
        }
        
        # Métriques
        self.metrics = MetricsCollector()
    
    def on_tick(self, tick: TickData) -> None:
        """
        Traite un tick et met à jour le VWAP
        TickData: {'timestamp': datetime, 'bid': float, 'ask': float, 'last': float, 'volume': int}
        """
        # Validation
        if not self._validate_tick(tick):
            self.metrics.increment('invalid_ticks')
            return
        
        # Check reset quotidien
        self._check_reset(tick['timestamp'])
        
        # Calcul prix typique
        typical_price = (tick['bid'] + tick['ask'] + tick['last']) / 3.0
        volume = tick.get('volume', 1)  # Fallback à 1 si pas de volume
        
        # Mise à jour cumulative (formule récursive pour éviter overflow)
        self.cumulative_tpv += typical_price * volume
        self.cumulative_volume += volume
        
        # Calcul VWAP
        if self.cumulative_volume > 0:
            self.current_vwap = self.cumulative_tpv / self.cumulative_volume
        
        # Mise à jour historique
        self._update_history(tick['timestamp'], self.current_vwap)
        
        # Mise à jour cache
        self._update_cache(tick['timestamp'])
        
        # Métriques
        self.metrics.record_latency('tick_processing', start_time)
    
    def _validate_tick(self, tick: dict) -> bool:
        """Validation rigoureuse du tick"""
        checks = [
            ('timestamp' in tick and isinstance(tick['timestamp'], datetime)),
            ('bid' in tick and 0 < tick['bid'] < 100000),
            ('ask' in tick and 0 < tick['ask'] < 100000),
            ('last' in tick and 0 < tick['last'] < 100000),
            (tick['ask'] > tick['bid']),  # Spread positif
            (abs(tick['last'] - (tick['bid'] + tick['ask'])/2) < 100),  # Pas d'outlier extrême
        ]
        return all(checks)
    
    @numba.jit(nopython=True, fastmath=True)
    def _calculate_vwap_batch(self, typical_prices: np.ndarray, volumes: np.ndarray) -> np.ndarray:
        """Calcul batch optimisé avec Numba"""
        n = len(typical_prices)
        vwap_array = np.zeros(n)
        cum_tpv = 0.0
        cum_vol = 0.0
        
        for i in range(n):
            cum_tpv += typical_prices[i] * volumes[i]
            cum_vol += volumes[i]
            if cum_vol > 0:
                vwap_array[i] = cum_tpv / cum_vol
            else:
                vwap_array[i] = typical_prices[i]  # Fallback
        
        return vwap_array
3.2 Module VWAP Derivatives
python
"""
Module: vwap_derivatives.py
Calcul des indicateurs dérivés du VWAP
"""

class VWAPDerivatives:
    """Calculateur d'indicateurs dérivés du VWAP"""
    
    def __init__(self, window_sizes: list = None):
        self.window_sizes = window_sizes or [20, 50, 100, 200]
        
    def calculate_all(self, vwap_series: np.ndarray, price_series: np.ndarray) -> dict:
        """Calcule tous les dérivés"""
        return {
            'slope': self._calculate_slope(vwap_series),
            'curvature': self._calculate_curvature(vwap_series),
            'distance': self._calculate_distance(vwap_series, price_series),
            'bands': self._calculate_bands(vwap_series),
            'velocity': self._calculate_velocity(vwap_series),
            'acceleration': self._calculate_acceleration(vwap_series),
            'regime': self._detect_regime(vwap_series, price_series),
            'divergence': self._detect_divergence(vwap_series, price_series),
        }
    
    @staticmethod
    @numba.jit(nopython=True)
    def _calculate_slope(series: np.ndarray, window: int = 20) -> float:
        """Calcule la pente avec régression linéaire"""
        if len(series) < window:
            return 0.0
        
        x = np.arange(window)
        y = series[-window:]
        
        # Formule fermée pour la régression
        n = len(x)
        sum_x = np.sum(x)
        sum_y = np.sum(y)
        sum_xy = np.sum(x * y)
        sum_xx = np.sum(x * x)
        
        slope = (n * sum_xy - sum_x * sum_y) / (n * sum_xx - sum_x * sum_x)
        return slope
    
    def _calculate_bands(self, vwap_series: np.ndarray, std_dev: list = [1, 2]) -> dict:
        """Calcule les bandes de Bollinger autour du VWAP"""
        bands = {}
        for std in std_dev:
            rolling_mean = pd.Series(vwap_series).rolling(window=20).mean()
            rolling_std = pd.Series(vwap_series).rolling(window=20).std()
            
            bands[f'upper_{std}std'] = rolling_mean + (rolling_std * std)
            bands[f'lower_{std}std'] = rolling_mean - (rolling_std * std)
        
        return bands
    
    def _detect_regime(self, vwap_series: np.ndarray, price_series: np.ndarray) -> str:
        """Détecte le régime de marché"""
        if len(vwap_series) < 50:
            return 'unknown'
        
        # Distance moyenne
        avg_distance = np.mean(np.abs(price_series[-50:] - vwap_series[-50:]))
        
        # Volatilité relative
        vwap_volatility = np.std(vwap_series[-20:])
        price_volatility = np.std(price_series[-20:])
        vol_ratio = vwap_volatility / price_volatility if price_volatility > 0 else 1.0
        
        # Détection de régime
        if avg_distance < 0.5 and vol_ratio < 0.3:
            return 'accumulation'
        elif avg_distance > 2.0 and vol_ratio > 0.7:
            return 'trending'
        elif 0.3 < vol_ratio < 0.7:
            return 'balanced'
        else:
            return 'transitional'
3.3 Module VWAP Signals
python
"""
Module: vwap_signals.py
Génération de signaux de trading basés sur le VWAP
"""

class VWAPSignalGenerator:
    """Générateur de signaux institutionnels"""
    
    SIGNAL_TYPES = {
        'VWAP_CROSS': 1,        # Prix croise VWAP
        'VWAP_REJECTION': 2,    # Rejet du VWAP
        'BAND_TOUCH': 3,        # Touche une bande
        'DIVERGENCE': 4,        # Divergence prix/VWAP
        'REGIME_CHANGE': 5,     # Changement de régime
    }
    
    def __init__(self, config: dict):
        self.thresholds = config.get('thresholds', {
            'neutral_zone': 2.0,  # $ pour XAUUSD
            'strong_zone': 5.0,
            'extreme_zone': 8.0,
            'slope_threshold': 0.001,
            'min_confidence': 0.7,
        })
        
    def generate_signal(self, context: dict) -> Signal:
        """Génère un signal complet"""
        signal = {
            'timestamp': datetime.utcnow(),
            'symbol': context['symbol'],
            'type': None,
            'strength': 0.0,
            'confidence': 0.0,
            'action': 'HOLD',  # BUY, SELL, HOLD
            'metadata': {},
            'triggers': []
        }
        
        # Analyse distance
        distance = context['price'] - context['vwap']
        distance_pips = distance / context['pip_value']
        
        # Classification
        if abs(distance_pips) <= self.thresholds['neutral_zone']:
            zone = 'NEUTRAL'
            multiplier = 1.0
        elif abs(distance_pips) <= self.thresholds['strong_zone']:
            zone = 'STRONG'
            multiplier = 1.5 if (distance > 0) == (context['slope'] > 0) else 0.8
        else:
            zone = 'EXTREME'
            multiplier = 2.0 if (distance > 0) == (context['slope'] > 0) else 0.5
        
        # Signal de tendance
        if context['slope'] > self.thresholds['slope_threshold']:
            trend = 'BULLISH'
        elif context['slope'] < -self.thresholds['slope_threshold']:
            trend = 'BEARISH'
        else:
            trend = 'FLAT'
        
        # Génération du signal principal
        if zone == 'EXTREME' and trend != 'FLAT':
            signal['type'] = self.SIGNAL_TYPES['VWAP_REJECTION']
            signal['action'] = 'SELL' if distance > 0 else 'BUY'
            signal['strength'] = 0.8 * multiplier
            signal['confidence'] = 0.75
        
        # Ajout de triggers secondaires
        if 'bands' in context:
            signal['triggers'].extend(
                self._check_bands(context['price'], context['bands'])
            )
        
        return signal
    
    def _check_bands(self, price: float, bands: dict) -> list:
        """Vérifie les interactions avec les bandes"""
        triggers = []
        
        for band_name, band_value in bands.items():
            if band_value is None:
                continue
            
            distance_to_band = abs(price - band_value)
            if distance_to_band < 0.5:  # 50 cents
                triggers.append({
                    'type': 'BAND_PROXIMITY',
                    'band': band_name,
                    'distance': distance_to_band,
                    'strength': 0.3
                })
        
        return triggers
3.4 Module Cache Manager
python
"""
Module: vwap_cache.py
Gestionnaire de cache multi-niveaux
"""

class VWAPCacheManager:
    """Cache manager L1/L2/L3"""
    
    def __init__(self, config: dict):
        # L1: In-memory (ultra-rapide)
        self.l1_cache = {
            'current': {},      # Valeurs actuelles
            'history': {},      # Historique court
            'signals': {},      # Signaux récents
        }
        
        # L2: Redis (cross-process)
        self.redis_client = redis.Redis(
            host=config.get('redis_host', 'localhost'),
            port=config.get('redis_port', 6379),
            decode_responses=True
        )
        
        # L3: Database (persistent)
        self.db_engine = create_engine(config['database_url'])
        
        # Configuration
        self.ttl = {
            'l1': timedelta(seconds=1),
            'l2': timedelta(minutes=5),
            'l3': timedelta(days=30),
        }
        
    def get(self, key: str, max_age: timedelta = None) -> dict:
        """Récupère depuis le cache avec fallback"""
        # Essaye L1
        if key in self.l1_cache['current']:
            item = self.l1_cache['current'][key]
            if datetime.utcnow() - item['timestamp'] < self.ttl['l1']:
                item['cache_hit'] = 'L1'
                return item
        
        # Essaye L2
        redis_key = f"vwap:{key}"
        redis_data = self.redis_client.get(redis_key)
        if redis_data:
            item = json.loads(redis_data)
            if datetime.utcnow() - datetime.fromisoformat(item['timestamp']) < self.ttl['l2']:
                # Populate L1
                self.l1_cache['current'][key] = item
                item['cache_hit'] = 'L2'
                return item
        
        # Fallback à L3/database
        item = self._fetch_from_db(key)
        if item:
            # Populate L2 et L1
            self.redis_client.setex(redis_key, self.ttl['l2'], json.dumps(item))
            self.l1_cache['current'][key] = item
            item['cache_hit'] = 'L3'
        
        return item
    
    def set(self, key: str, value: dict) -> None:
        """Met à jour tous les niveaux de cache"""
        value['timestamp'] = datetime.utcnow().isoformat()
        value['version'] = '1.0'
        
        # L1
        self.l1_cache['current'][key] = value
        
        # L2
        redis_key = f"vwap:{key}"
        self.redis_client.setex(
            redis_key,
            int(self.ttl['l2'].total_seconds()),
            json.dumps(value)
        )
        
        # L3 async
        asyncio.create_task(self._persist_to_db(key, value))
3.5 Module Integration Hooks
python
"""
Module: vwap_integration.py
Hooks d'intégration avec le pipeline existant
"""

class VWAPIntegration:
    """Intégration avec SNIPER-X"""
    
    def __init__(self, pipeline_context: dict):
        self.context = pipeline_context
        self.vwap_engine = VWAPCalculator(config)
        self.signal_gen = VWAPSignalGenerator(config)
        self.cache = VWAPCacheManager(config)
        
        # Integration points
        self.integration_points = {
            'market_analyzer': self._hook_market_analyzer,
            'fusion_manager': self._hook_fusion_manager,
            'decision_pipeline': self._hook_decision_pipeline,
            'trade_executor': self._hook_trade_executor,
        }
    
    def _hook_market_analyzer(self, analysis_result: dict) -> dict:
        """Hook pour enrichir MarketAnalyzer"""
        vwap_context = self._get_vwap_context(analysis_result['symbol'])
        
        # Ajout des données VWAP
        analysis_result['vwap'] = {
            'value': vwap_context['current'],
            'distance_pips': vwap_context['distance_pips'],
            'zone': vwap_context['zone'],
            'slope': vwap_context['slope'],
            'regime': vwap_context['regime'],
            'bands': vwap_context.get('bands', {}),
        }
        
        return analysis_result
    
    def _hook_fusion_manager(self, fusion_input: dict) -> dict:
        """Hook pour FusionManager V2"""
        # Récupère le contexte VWAP
        symbol = fusion_input.get('symbol', 'XAUUSD')
        vwap_data = self.cache.get(f"{symbol}:vwap_context")
        
        if not vwap_data:
            return fusion_input
        
        # Applique le multiplicateur VWAP
        base_score = fusion_input.get('base_score', 0.5)
        vwap_multiplier = self._calculate_vwap_multiplier(
            vwap_data,
            fusion_input.get('action')
        )
        
        # Score ajusté
        adjusted_score = base_score * vwap_multiplier
        
        # Met à jour le résultat
        fusion_input['vwap_context'] = vwap_data
        fusion_input['vwap_multiplier'] = vwap_multiplier
        fusion_input['adjusted_score'] = adjusted_score
        
        # Ajoute des métadonnées
        fusion_input['metadata']['vwap_zone'] = vwap_data['zone']
        fusion_input['metadata']['vwap_distance'] = vwap_data['distance_pips']
        
        return fusion_input
    
    def _calculate_vwap_multiplier(self, vwap_data: dict, action: str) -> float:
        """Calcule le multiplicateur basé sur l'alignement VWAP"""
        zone = vwap_data['zone']
        distance = vwap_data['distance_pips']
        slope = vwap_data['slope']
        
        # Règles de base
        if zone == 'NEUTRAL':
            return 1.0
        
        # Détermine l'alignement
        is_aligned = (
            (distance > 0 and action == 'BUY' and slope > 0) or
            (distance < 0 and action == 'SELL' and slope < 0)
        )
        
        if is_aligned:
            if zone == 'STRONG':
                return 1.5
            elif zone == 'EXTREME':
                return 2.0
        else:
            if zone == 'STRONG':
                return 0.8
            elif zone == 'EXTREME':
                return 0.5
        
        return 1.0
4. 🗃️ STRUCTURE DE DONNÉES
4.1 Classes principales
python
@dataclass
class VWAPTick:
    """Structure d'un tick pour le calcul VWAP"""
    timestamp: datetime
    symbol: str
    bid: Decimal
    ask: Decimal
    last: Decimal
    volume: int
    source: str = "MT5"
    sequence_id: int = 0
    is_valid: bool = True
    
    def to_dict(self) -> dict:
        return asdict(self)

@dataclass
class VWAPState:
    """État complet du calcul VWAP"""
    symbol: str
    session_date: date
    cumulative_tpv: Decimal = Decimal('0.0')
    cumulative_volume: Decimal = Decimal('0.0')
    current_vwap: Decimal = Decimal('0.0')
    session_high: Decimal = Decimal('0.0')
    session_low: Decimal = Decimal('0.0')
    session_vwap: Decimal = Decimal('0.0')
    tick_count: int = 0
    last_update: datetime = None
    
    def reset(self, new_date: date) -> None:
        """Réinitialise pour une nouvelle session"""
        self.session_date = new_date
        self.cumulative_tpv = Decimal('0.0')
        self.cumulative_volume = Decimal('0.0')
        self.current_vwap = Decimal('0.0')
        self.tick_count = 0

@dataclass
class VWAPDerivatives:
    """Dérivés calculés"""
    timestamp: datetime
    symbol: str
    slope_20: float
    slope_50: float
    curvature: float
    velocity: float
    acceleration: float
    distance_pips: float
    zone: str
    regime: str
    bands: Dict[str, float]
    confidence: float
    
    @property
    def is_bullish(self) -> bool:
        return self.slope_20 > 0 and self.velocity > 0
    
    @property
    def is_bearish(self) -> bool:
        return self.slope_20 < 0 and self.velocity < 0
4.2 Base de données - Schéma SQL
sql
-- Table principale VWAP
CREATE TABLE vwap_ticks (
    id BIGSERIAL PRIMARY KEY,
    timestamp TIMESTAMPTZ NOT NULL,
    symbol VARCHAR(10) NOT NULL,
    bid DECIMAL(12, 5) NOT NULL,
    ask DECIMAL(12, 5) NOT NULL,
    last DECIMAL(12, 5) NOT NULL,
    volume INTEGER NOT NULL,
    typical_price DECIMAL(12, 5) GENERATED ALWAYS AS ((bid + ask + last) / 3) STORED,
    source VARCHAR(20) NOT NULL,
    sequence_id BIGINT NOT NULL,
    is_valid BOOLEAN DEFAULT TRUE,
    created_at TIMESTAMPTZ DEFAULT NOW()
);

-- Index pour performances
CREATE INDEX idx_vwap_ticks_symbol_time ON vwap_ticks (symbol, timestamp DESC);
CREATE INDEX idx_vwap_ticks_sequence ON vwap_ticks (symbol, sequence_id);

-- Table VWAP calculé
CREATE TABLE vwap_calculated (
    id BIGSERIAL PRIMARY KEY,
    timestamp TIMESTAMPTZ NOT NULL,
    symbol VARCHAR(10) NOT NULL,
    session_date DATE NOT NULL,
    vwap_value DECIMAL(12, 5) NOT NULL,
    cumulative_tpv DECIMAL(20, 5) NOT NULL,
    cumulative_volume DECIMAL(20, 5) NOT NULL,
    tick_count INTEGER NOT NULL,
    derived_data JSONB,
    created_at TIMESTAMPTZ DEFAULT NOW(),
    
    UNIQUE(symbol, timestamp)
) PARTITION BY RANGE (timestamp);

-- Table des signaux
CREATE TABLE vwap_signals (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    timestamp TIMESTAMPTZ NOT NULL,
    symbol VARCHAR(10) NOT NULL,
    signal_type VARCHAR(20) NOT NULL,
    action VARCHAR(10) NOT NULL,
    strength DECIMAL(5, 3) NOT NULL,
    confidence DECIMAL(5, 3) NOT NULL,
    vwap_context JSONB NOT NULL,
    market_context JSONB,
    triggered_by VARCHAR(50),
    processed BOOLEAN DEFAULT FALSE,
    processed_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ DEFAULT NOW()
);
5. 🔧 CONFIGURATION
5.1 Fichier de configuration principal
json
{
  "vwap_module": {
    "version": "1.0.0",
    "mode": "production",
    "symbols": ["XAUUSD", "EURUSD", "GBPUSD"],
    
    "calculation": {
      "reset_hour": 0,
      "reset_timezone": "GMT",
      "pip_value": 0.01,
      "decimal_places": 2,
      "typical_price_formula": "(bid + ask + last) / 3",
      "volume_source": "tick_volume",
      "min_ticks_for_valid_vwap": 10
    },
    
    "derivatives": {
      "window_sizes": [20, 50, 100, 200],
      "bands_std_dev": [1.0, 2.0],
      "slope_threshold": 0.001,
      "curvature_window": 50
    },
    
    "signals": {
      "neutral_zone_pips": 200,
      "strong_zone_pips": 500,
      "extreme_zone_pips": 800,
      "min_confidence": 0.7,
      "multipliers": {
        "aligned_neutral": 1.0,
        "aligned_strong": 1.5,
        "aligned_extreme": 2.0,
        "against_neutral": 0.8,
        "against_strong": 0.5,
        "against_extreme": 0.3
      }
    },
    
    "cache": {
      "l1_ttl_seconds": 1,
      "l2_ttl_seconds": 300,
      "l3_ttl_days": 30,
      "redis_host": "localhost",
      "redis_port": 6379,
      "redis_db": 0
    },
    
    "performance": {
      "max_tick_queue_size": 10000,
      "processing_batch_size": 1000,
      "max_processing_time_ms": 10,
      "enable_async_processing": true,
      "num_worker_threads": 4
    },
    
    "monitoring": {
      "metrics_port": 9090,
      "health_check_interval": 30,
      "alert_on_latency_ms": 100,
      "alert_on_memory_mb": 1024,
      "log_level": "INFO"
    },
    
    "integration": {
      "fusion_manager_weight": 0.3,
      "enable_market_analyzer_hook": true,
      "enable_decision_pipeline_hook": true,
      "enable_trade_executor_hook": true,
      "zero_mq_port": 5555,
      "web_socket_port": 8765
    }
  }
}