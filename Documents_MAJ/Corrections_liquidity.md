RAPPORT D'AMÉLIORATION - STRATÉGIE LIQUIDITY
1. BUGS CRITIQUES BLOQUANTS
BUG #1 : Indentation fatale dans Setup BOS/MSS + Absorption
Fichier : strategy/liquidity.py
Fonction : _evaluate_single_asset
Localisation : Lignes ~595-640

Problème :

python
if bos_mss and absorption:
    bos_type = bos_mss.get("type", "").lower()
    abs_side = absorption.get("side", "").lower()

    # BUY Setup : BOS bullish + Absorption buy-side
    if "bull" in bos_type and abs_side == "buy":
        # ... 15 lignes de code BUY ...

    # SELL Setup : BOS bearish + Absorption sell-side
        bos_level = float(bos_mss.get("level", price))  # <-- INDENTÉ À L'INTÉRIEUR DU IF BUY!
        # ... reste du code SELL ...
Impact : Tous les setups SELL de type BOS/MSS + Absorption sont ignorés. Le code SELL s'exécute seulement si le setup BUY est valide (ce qui est impossible).

Correction nécessaire :

python
if bos_mss and absorption:
    bos_type = bos_mss.get("type", "").lower()
    abs_side = absorption.get("side", "").lower()

    # BUY Setup : BOS bullish + Absorption buy-side
    if "bull" in bos_type and abs_side == "buy":
        # ... code BUY ...

    # SELL Setup : BOS bearish + Absorption sell-side  <-- MÊME NIVEL QUE LE IF BUY
    elif "bear" in bos_type and abs_side == "sell":  # <-- AJOUTER 'elif'
        bos_level = float(bos_mss.get("level", price))
        # ... code SELL ...
BUG #2 : Variable 'sl' non définie dans Micro Phase Reversal
Fichier : strategy/liquidity.py
Fonction : _evaluate_single_asset
Localisation : Lignes ~680-700

Problème :

python
# SELL Setup : Micro phase = distribution + Regime favorable
elif micro_phase == "distribution" and regime in ["trending_down", "transitional"]:
    entry = price
    tp = entry - (60 * pip_size)  # TP 60 pips
    
    sl_pips = abs(sl - entry) / pip_size  # <-- ERREUR: 'sl' N'EXISTE PAS!
    # Variable 'sl' jamais initialisée
Impact : Erreur NameError à l'exécution, setup SELL Micro Phase jamais déclenché.

Correction nécessaire :

python
elif micro_phase == "distribution" and regime in ["trending_down", "transitional"]:
    entry = price
    sl = entry + (30 * pip_size)  # <-- AJOUTER CETTE LIGNE (30 pips SL)
    tp = entry - (60 * pip_size)
    
    sl_pips = abs(sl - entry) / pip_size  # <-- MAINTENANT 'sl' EST DÉFINI
    tp_pips = abs(entry - tp) / pip_size
    rr = tp_pips / sl_pips if sl_pips > 0 else 0
BUG #3 : Problème de logique dans Setup BOS/MSS + Absorption
Localisation : Lignes ~635-645

Problème :

python
# Dans le bloc SELL (après correction d'indentation):
if body_ratio >= 0.6:
    entry = price
    sl = bos_level + (20 * pip_size)  # SL au-dessus du BOS
    sl_pips = abs(sl - entry) / pip_size
    tp = entry - (sl_pips * 2.0 * pip_size)  # <-- LOGIQUE INVERSE!
Impact : Si bos_level est au-dessus du prix actuel (ce qui est typique pour un BOS bearish), le SL est encore plus haut, mais le TP est calculé comme entry - (distance * 2) ce qui donne un TP PLUS BAS que l'entrée pour un trade SELL (correct), mais le RR peut être erroné.

Correction recommandée :

python
# Remplacer la ligne problématique par:
tp = entry - (sl_pips * 2.0 * pip_size)  # Déjà correct pour SELL
# Vérifier que sl_pips est positif
if sl_pips > 0:
    rr = 2.0  # Fixe à 2.0 plutôt que recalcul
2. PROBLÈMES DE FOND
PROBLÈME #1 : Validation trop stricte des DataFrames
Fonction : _validate_dataframe
Localisation : Ligne ~130

Problème :

python
def _validate_dataframe(self, df: pd.DataFrame, required_cols: List[str]) -> bool:
    if df is None or df.empty:
        return False
    missing_cols = set(required_cols) - set(df.columns)
    return len(missing_cols) == 0  # <-- RIGIDE: manque 1 colonne = échec total
Impact : Si une seule colonne manque (ex: tick_volume), toute la détection est abandonnée.

Amélioration suggérée :

python
def _validate_dataframe(self, df: pd.DataFrame, required_cols: List[str]) -> bool:
    if df is None or df.empty or len(df) < 20:
        self.logger.warning(f"DataFrame invalide: None/empty ou <20 rows")
        return False
    
    missing_cols = [col for col in required_cols if col not in df.columns]
    if missing_cols:
        self.logger.warning(f"Colonnes manquantes: {missing_cols}")
        # Pour les colonnes non critiques, continuer quand même
        critical_cols = ["open", "high", "low", "close"]
        if any(col in missing_cols for col in critical_cols):
            return False
    return True
PROBLÈME #2 : Gestion d'erreurs trop générique
Localisation : Multiple (dans _detect_liquidity_signals)

Problème :

python
try:
    # Appel détecteur
except (AttributeError, KeyError, TypeError, ValueError) as e:
    self.logger.debug(f"[{asset}] detect_X error: {e}")
except Exception as e:  # <-- CAPTURE TOUT!
    self.logger.error(f"[{asset}] detect_X unexpected error: {e}", exc_info=True)
Impact : Masque des erreurs importantes, rend le débogage difficile.

Amélioration suggérée :

python
try:
    # Appel détecteur
except (AttributeError, KeyError, TypeError, ValueError) as e:
    self.logger.warning(f"[{asset}] Erreur de données dans {detector_name}: {e}")
    # Log supplémentaire selon niveau de debug
    if self.logger.level <= 10:  # DEBUG
        self.logger.debug(f"DataFrame shape: {df.shape}, columns: {list(df.columns)}")
except Exception as e:
    self.logger.error(f"[{asset}] ERREUR CRITIQUE dans {detector_name}: {e}", exc_info=True)
    # Ne pas propager pour ne pas bloquer toute la stratégie
PROBLÈME #3 : Manque de métriques de performance
Impact : Impossible de savoir pourquoi aucun trade n'est généré.

Amélioration suggérée : Ajouter des compteurs statistiques :

python
class LiquidityStrategy(BaseStrategy):
    def __init__(self, ...):
        super().__init__(...)
        self.stats = {
            "total_evaluations": 0,
            "signals_detected": 0,
            "setup1_valid": 0,
            "setup2_valid": 0,
            # ... autres setups
            "rejections": {
                "no_sweep": 0,
                "no_eqh_eql": 0,
                "distance_too_far": 0,
                "rr_insufficient": 0
            }
        }
    
    def _evaluate_single_asset(self, ...):
        self.stats["total_evaluations"] += 1
        # ... dans chaque condition de rejet:
        if not sweep:
            self.stats["rejections"]["no_sweep"] += 1
3. OPTIMISATIONS RECOMMANDÉES
OPTIMISATION #1 : Cache des résultats de détection
Problème : Les 8 détecteurs sont appelés à chaque évaluation (potentiellement chaque tick).

Solution :

python
def _detect_liquidity_signals(self, asset, df, df_htf=None):
    # Générer une clé de cache basée sur le hash des données
    cache_key = f"{asset}_{df.index[-1]}_{df.shape[0]}"
    
    if hasattr(self, '_signal_cache') and cache_key in self._signal_cache:
        if time.time() - self._signal_cache[cache_key]['timestamp'] < 60:  # 60s cache
            return self._signal_cache[cache_key]['signals']
    
    # Calcul normal...
    self._signal_cache[cache_key] = {
        'signals': signals,
        'timestamp': time.time()
    }
    
    # Nettoyer le cache (garder seulement les 10 dernières entrées)
    if len(self._signal_cache) > 10:
        oldest_key = min(self._signal_cache.keys(), 
                        key=lambda k: self._signal_cache[k]['timestamp'])
        del self._signal_cache[oldest_key]
    
    return signals
OPTIMISATION #2 : Logging conditionnel pour production
Problème : Trop de logs DEBUG en production.

Solution :

python
def _log_liquidity_consolidated_report(self, asset, ...):
    # N'afficher que si DEBUG ou si setup valide détecté
    if self.logger.level <= 10 or decision is not None:  # DEBUG ou trade
        # Log complet
    else:
        # Log minimal
        self.logger.info(f"[LIQUIDITY][{asset}] Évaluation: {len(liquidity_signals)} signaux")
4. PLAN D'ACTION PRIORITAIRE
PHASE 1 : Corrections critiques (30 minutes)
Corriger l'indentation BUG #1 - Setup BOS/MSS + Absorption

Ajouter variable manquante - BUG #2 Micro Phase SELL

Vérifier la logique de calcul TP/SL - BUG #3

PHASE 2 : Améliorations monitoring (15 minutes)
Ajouter statistiques de performance

Configurer logging différentiel (DEBUG en test, INFO en prod)

Ajouter checkpoint logging dans _evaluate_single_asset

PHASE 3 : Tests de validation (variable)
Mode test forcé : Simuler des conditions de marché connues

Backtest rapide sur 1 semaine de données

Validation manuelle de quelques setups détectés

5. CODE DE PATCH COMPLET
python
# À insérer dans _evaluate_single_asset, remplaçant les sections problématiques:

# ========== CORRECTION SETUP 2 (BOS/MSS + Absorption) ==========
if bos_mss and absorption:
    bos_type = bos_mss.get("type", "").lower()
    abs_side = absorption.get("side", "").lower()

    # BUY Setup : BOS bullish + Absorption buy-side
    if "bull" in bos_type and abs_side == "buy":
        bos_level = float(bos_mss.get("level", price))
        body_ratio = float(absorption.get("body_ratio", 0.0))

        if body_ratio >= 0.6:
            entry = price
            sl = bos_level - (20 * pip_size)
            sl_pips = abs(entry - sl) / pip_size
            tp = entry + (sl_pips * 2.0 * pip_size)

            tp_pips = abs(tp - entry) / pip_size
            rr = tp_pips / sl_pips if sl_pips > 0 else 0

            self.logger.info(f"[LIQUIDITY][{asset}] ⚡ SETUP VALIDE | bos_absorption_buy")
            # ... construction decision ...

    # SELL Setup : BOS bearish + Absorption sell-side - CORRIGÉ
    elif "bear" in bos_type and abs_side == "sell":
        bos_level = float(bos_mss.get("level", price))
        body_ratio = float(absorption.get("body_ratio", 0.0))

        if body_ratio >= 0.6:
            entry = price
            sl = bos_level + (20 * pip_size)
            sl_pips = abs(sl - entry) / pip_size
            tp = entry - (sl_pips * 2.0 * pip_size)

            tp_pips = abs(entry - tp) / pip_size
            rr = tp_pips / sl_pips if sl_pips > 0 else 0

            self.logger.info(f"[LIQUIDITY][{asset}] ⚡ SETUP VALIDE | bos_absorption_sell")
            # ... construction decision ...

# ========== CORRECTION SETUP 5 (Micro Phase Reversal SELL) ==========
elif micro_phase == "distribution" and regime in ["trending_down", "transitional"]:
    entry = price
    sl = entry + (30 * pip_size)  # AJOUTÉ - SL 30 pips au-dessus
    tp = entry - (60 * pip_size)
    
    sl_pips = abs(sl - entry) / pip_size
    tp_pips = abs(entry - tp) / pip_size
    rr = tp_pips / sl_pips if sl_pips > 0 else 0

    self.logger.info(f"[LIQUIDITY][{asset}] ⚡ SETUP VALIDE | micro_phase_reversal_sell")
    # ... construction decision ...
Temps estimé pour corrections : 45-60 minutes
Impact attendu : Déblocage immédiat des setups SELL et résolution des erreurs d'exécution
Risque résiduel : Configuration des seuils (50 pips max) peut être trop restrictive pour certains instruments

