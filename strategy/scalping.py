# scalping.py — version Burst-Only (no Bollinger / no Katana)
from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple
import math
import time
import uuid
from .base_strategy import BaseStrategy
import numpy as np
import pandas as pd

class ScalpingStrategy(BaseStrategy):
    """
    Stratégie SCALPING focalisée sur :
    1) Burst Scalping (basket d’ordres simultanés) — priorité
    2) Liquidity Sweep (cassures HH/LL récentes) — optionnel

    ➤ AUCUNE règle basée sur la lecture de chandeliers (marubozu, patterns, etc.).
    ➤ Les tailles (volume) sont déléguées au TradeExecutor (risk-based).
    ➤ Les SL/TP sont gérés par le moteur SL/TP (RR dynamique) côté exécuteur.
    """


    def __init__(
        self,
        config_manager,
        strategy_config: Optional[Dict[str, Any]] = None,
        mt5_connector=None,  # 👈 ajouté ici
        logger=None,
    ):
        """
        Initialise la stratégie Scalping.
        """
        super().__init__(config_manager, strategy_config or {})

        self.config_manager = config_manager
        self.strategy_config = strategy_config or {}
        self.mt5_connector = mt5_connector  # ✅ plus d'erreur
        self.logger = logger or getattr(config_manager, "logger", None)

        # === CODE MORT SUPPRIMÉ (Session 23 Nov 2025) ===
        # L'instance Detectors n'était JAMAIS utilisée (grep "self.detectors." → 0 résultats)
        # Scalping utilise FootprintAnalyzer qui importe les 7 fonctions standalone
        # Les 8 méthodes de classe Detectors sont réservées à LiquidityStrategy

        self.logger.info("Moteur de stratégie Scalping initialisé.")

    # ==========================================================
    # =============   API PRINCIPALE (ENTRÉE)   ================
    # ==========================================================
    # --- Dans class ScalpingStrategy(BaseStrategy): ---
    def _finalize_decision(
        self, decision: Dict[str, Any], analyzed_context: Dict[str, Any]
    ) -> Dict[str, Any]:
        """
        Normalise la décision avant retour au pipeline.
        Évite que le pipeline écrase une décision valide faute de champs attendus.
        """
        if not isinstance(decision, dict):
            return {}

        # Champs standard attendus par l'executor / pipeline
        decision.setdefault("strategy_type", "scalping")
        decision.setdefault("rule_name", decision.get("rule_name", "burst_scalping"))
        decision.setdefault("execution_status", "ready")  # prêt à exécuter
        decision.setdefault("confidence", float(decision.get("confidence", 0.0) or 0.0))

        # Optionnel: petit snapshot de contexte utile au debug
        ctx = analyzed_context or {}
        decision.setdefault(
            "context_snapshot",
            {
                "cycle": ctx.get("cycle_count"),
                "daily_trade_count": ctx.get("daily_trade_count"),
                "market_regime": ctx.get("market_regime"),
            },
        )

        return decision

    # ==========================================================
    # =========   ORDERFLOW V6 SCORING SYSTEM   ================
    # ==========================================================

    def _analyze_orderflow_v6(
        self,
        asset: str,
        df_m1: pd.DataFrame,
        df_m5: Optional[pd.DataFrame],
        df_m15: Optional[pd.DataFrame],
        asset_signals: Dict[str, Any]
    ) -> Dict[str, Any]:
        """
        📈 OrderFlow Analysis V6 - Multi-Timeframe

        Périodes STRICTES :
        • M1 : 8 bougies → Momentum immédiat
        • M5 : 6 bougies → Structure court terme
        • M15 : 4 bougies → Contexte moyen terme

        Focus principal :
        • Volume Profile : 15 bougies M1
        • Delta Analysis : 10 bougies M1
        • Imbalances : 5 bougies M1 + 8 bougies M5

        Retourne :
        {
            "delta_momentum_score": 0-25,
            "volume_confirmation_score": 0-15,
            "imbalance_strength_score": 0-10,
            "total_score": 0-50,
            "mtf_alignment": {"m1": "", "m5": "", "m15": ""},
            "details": {...}
        }
        """
        result = {
            "delta_momentum_score": 0.0,
            "volume_confirmation_score": 0.0,
            "imbalance_strength_score": 0.0,
            "total_score": 0.0,
            "mtf_alignment": {"m1": "neutral", "m5": "neutral", "m15": "neutral"},
            "details": {}
        }

        try:
            # ================================================================
            # 0. ANALYSE MULTI-TIMEFRAME (M1/M5/M15)
            # ================================================================
            mtf_details = {}

            # M1 : 8 bougies → Momentum immédiat
            if df_m1 is not None and len(df_m1) >= 8:
                m1_closes = df_m1["close"].tail(8).values
                m1_opens = df_m1["open"].tail(8).values
                m1_bullish = sum(1 for i in range(len(m1_closes)) if m1_closes[i] > m1_opens[i])
                m1_bearish = 8 - m1_bullish

                if m1_bullish >= 6:  # 6/8 haussier
                    result["mtf_alignment"]["m1"] = "bullish"
                elif m1_bearish >= 6:  # 6/8 baissier
                    result["mtf_alignment"]["m1"] = "bearish"
                else:
                    result["mtf_alignment"]["m1"] = "neutral"

                mtf_details["m1"] = {
                    "bullish_bars": m1_bullish,
                    "bearish_bars": m1_bearish,
                    "direction": result["mtf_alignment"]["m1"]
                }

            # M5 : 6 bougies → Structure court terme
            if df_m5 is not None and len(df_m5) >= 6:
                m5_closes = df_m5["close"].tail(6).values
                m5_opens = df_m5["open"].tail(6).values
                m5_bullish = sum(1 for i in range(len(m5_closes)) if m5_closes[i] > m5_opens[i])
                m5_bearish = 6 - m5_bullish

                if m5_bullish >= 5:  # 5/6 haussier
                    result["mtf_alignment"]["m5"] = "bullish"
                elif m5_bearish >= 5:  # 5/6 baissier
                    result["mtf_alignment"]["m5"] = "bearish"
                else:
                    result["mtf_alignment"]["m5"] = "neutral"

                mtf_details["m5"] = {
                    "bullish_bars": m5_bullish,
                    "bearish_bars": m5_bearish,
                    "direction": result["mtf_alignment"]["m5"]
                }

            # M15 : 4 bougies → Contexte moyen terme
            if df_m15 is not None and len(df_m15) >= 4:
                m15_closes = df_m15["close"].tail(4).values
                m15_opens = df_m15["open"].tail(4).values
                m15_bullish = sum(1 for i in range(len(m15_closes)) if m15_closes[i] > m15_opens[i])
                m15_bearish = 4 - m15_bullish

                if m15_bullish >= 3:  # 3/4 haussier
                    result["mtf_alignment"]["m15"] = "bullish"
                elif m15_bearish >= 3:  # 3/4 baissier
                    result["mtf_alignment"]["m15"] = "bearish"
                else:
                    result["mtf_alignment"]["m15"] = "neutral"

                mtf_details["m15"] = {
                    "bullish_bars": m15_bullish,
                    "bearish_bars": m15_bearish,
                    "direction": result["mtf_alignment"]["m15"]
                }

            result["details"]["mtf"] = mtf_details

            # Vérifier alignement multi-timeframe (bonus potentiel)
            mtf_aligned = False
            if (result["mtf_alignment"]["m1"] == result["mtf_alignment"]["m5"] == result["mtf_alignment"]["m15"]
                and result["mtf_alignment"]["m1"] != "neutral"):
                mtf_aligned = True
                result["mtf_aligned"] = True
                self.logger.debug(f"[{asset}] 🎯 MTF Alignment: {result['mtf_alignment']['m1'].upper()}")

            # ================================================================
            # 1. DELTA MOMENTUM (25 points max)
            # ================================================================
            delta_momentum_score = 0.0
            delta_details = {}

            # ✅ FIX (1er Décembre 2025): orchestrator stocke SEULEMENT "summary" (pas la structure complète)
            # Donc fp_raw contient directement {delta_total, buy_volume, ...} sans imbrication
            fp_raw = asset_signals.get("footprint_summary", {})

            # Vérifier si c'est une structure imbriquée (avec "summary") ou directe
            if isinstance(fp_raw, dict):
                if "summary" in fp_raw:
                    # Structure complète (depuis fusion_manager ou autre source)
                    fp_summary = fp_raw["summary"]
                else:
                    # Structure directe (depuis orchestrator)
                    fp_summary = fp_raw
            else:
                fp_summary = {}

            delta_total = 0
            if isinstance(fp_summary, dict):
                delta_total = float(fp_summary.get("delta_total", 0))

            # Stocker delta_total TOUJOURS (pour le rapport)
            delta_details["delta_total"] = delta_total

            # Analyser cohérence delta sur 10 bougies M1
            if df_m1 is not None and len(df_m1) >= 10:
                # Compter bougies avec delta cohérent
                closes = df_m1["close"].tail(10).values
                opens = df_m1["open"].tail(10).values
                bullish_count = sum(1 for i in range(len(closes)) if closes[i] > opens[i])
                bearish_count = sum(1 for i in range(len(closes)) if closes[i] < opens[i])

                coherence = max(bullish_count, bearish_count) / 10.0  # 0.0 à 1.0
                delta_details["coherence"] = coherence
                delta_details["bullish_bars"] = bullish_count
                delta_details["bearish_bars"] = bearish_count

                # Scoring Delta Momentum
                if abs(delta_total) > 0:
                    delta_direction = "bullish" if delta_total > 0 else "bearish"
                    delta_details["delta_total"] = delta_total
                    delta_details["direction"] = delta_direction

                    # Delta fort cohérent → 15-25 pts
                    if coherence >= 0.8:  # 8/10 bougies cohérentes
                        if abs(delta_total) >= 300:
                            delta_momentum_score = 25.0  # Très fort
                        elif abs(delta_total) >= 200:
                            delta_momentum_score = 20.0  # Fort
                        elif abs(delta_total) >= 100:
                            delta_momentum_score = 15.0  # Moyen
                    # Delta modéré → 5-10 pts
                    elif coherence >= 0.6:
                        delta_momentum_score = 10.0
                    else:
                        delta_momentum_score = 5.0
                else:
                    delta_details["direction"] = "neutral"
                    delta_momentum_score = 5.0

            result["delta_momentum_score"] = delta_momentum_score
            result["delta_momentum_details"] = delta_details  # FIX: Nom correct pour le rapport

            # ================================================================
            # 2. VOLUME CONFIRMATION (15 points max)
            # ================================================================
            volume_confirmation_score = 0.0
            volume_details = {}

            # ✅ DEBUG: Log colonnes df_m1
            if df_m1 is not None:
                self.logger.debug(f"[OF V6][{asset}] df_m1 columns: {list(df_m1.columns)[:10]}... (len={len(df_m1)})")

            # Vérifier si tick_volume existe, sinon essayer volume ou real_volume
            vol_col = None
            if df_m1 is not None and len(df_m1) >= 15:
                if "tick_volume" in df_m1.columns:
                    vol_col = "tick_volume"
                elif "volume" in df_m1.columns:
                    vol_col = "volume"
                elif "real_volume" in df_m1.columns:
                    vol_col = "real_volume"

            if vol_col is not None:
                self.logger.debug(f"[OF V6][{asset}] Utilisation colonne volume: {vol_col}")
                volumes = df_m1[vol_col].tail(15).values
                current_volume = volumes[-1]
                avg_volume = np.mean(volumes[:-1])  # Moyenne des 14 précédentes

                volume_ratio = current_volume / avg_volume if avg_volume > 0 else 1.0
                volume_details["current_volume"] = float(current_volume)
                volume_details["avg_volume"] = float(avg_volume)
                volume_details["ratio"] = volume_ratio

                # ✅ POC depuis footprint_summary (VRAI POC calculé depuis profil de volume)
                if isinstance(fp_summary, dict):
                    poc_price = fp_summary.get("poc")
                    if poc_price is not None and isinstance(poc_price, (int, float)):
                        volume_details["poc"] = float(poc_price)

                # Scoring Volume
                # ✅ FIX (2 Décembre 2025): Assouplissement seuils pour marché calme
                if volume_ratio >= 2.5:  # Volume spike
                    volume_confirmation_score = 15.0
                    volume_details["spike_detected"] = True
                elif volume_ratio >= 1.8:  # Volume fort
                    volume_confirmation_score = 12.0
                elif volume_ratio >= 1.5:  # Volume au-dessus moyenne
                    volume_confirmation_score = 10.0
                elif volume_ratio >= 1.0:  # Volume normal
                    volume_confirmation_score = 5.0
                elif volume_ratio >= 0.5:  # ✅ NOUVEAU: Marché calme mais actif
                    volume_confirmation_score = 3.0
                else:  # Volume très faible
                    volume_confirmation_score = 0.0

            result["volume_confirmation_score"] = volume_confirmation_score
            result["volume_confirmation_details"] = volume_details  # FIX: Nom correct pour le rapport

            # ================================================================
            # 3. IMBALANCE STRENGTH (10 points max)
            # ================================================================
            imbalance_strength_score = 0.0
            imbalance_details = {}

            # ✅ Récupérer imbalances depuis fp_summary (DÉJÀ calculées par footprint_validator)
            if isinstance(fp_summary, dict):
                imbalance_buy = int(fp_summary.get("imbalance_buy", 0))
                imbalance_sell = int(fp_summary.get("imbalance_sell", 0))
                total_imbalances = imbalance_buy + imbalance_sell

                imbalance_details["imbalance_buy"] = imbalance_buy
                imbalance_details["imbalance_sell"] = imbalance_sell
                imbalance_details["m1_count"] = total_imbalances  # Les imbalances footprint sont M1
                imbalance_details["m5_count"] = 0  # TODO: Si besoin M5 séparé, ajouter au footprint_validator
                imbalance_details["total_count"] = total_imbalances

                # Scoring basé sur les imbalances totales
                if total_imbalances >= 5:  # Beaucoup d'imbalances
                    imbalance_strength_score = 10.0
                elif total_imbalances >= 3:  # Imbalances significatives
                    imbalance_strength_score = 8.0
                elif total_imbalances >= 1:  # Imbalances mineures
                    imbalance_strength_score = 5.0
                else:
                    imbalance_strength_score = 0.0

            result["imbalance_strength_score"] = imbalance_strength_score
            result["imbalance_strength_details"] = imbalance_details  # FIX: Nom correct pour le rapport

            # ================================================================
            # TOTAL ORDERFLOW SCORE
            # ================================================================
            result["total_score"] = (
                delta_momentum_score +
                volume_confirmation_score +
                imbalance_strength_score
            )

            self.logger.debug(
                f"[{asset}] OrderFlow V6: Delta={delta_momentum_score:.1f} "
                f"Volume={volume_confirmation_score:.1f} "
                f"Imbalance={imbalance_strength_score:.1f} "
                f"→ Total={result['total_score']:.1f}/50"
            )

        except Exception as e:
            self.logger.error(f"[{asset}] OrderFlow V6 analysis error: {e}", exc_info=True)

        return result

    def calculate_orderflow_v6_standalone(
        self,
        asset: str,
        df_m1: pd.DataFrame,
        df_m5: Optional[pd.DataFrame],
        df_m15: Optional[pd.DataFrame],
        asset_signals: Dict[str, Any]
    ) -> Dict[str, Any]:
        """
        📈 Calcul OrderFlow V6 en mode standalone pour FusionManager.

        Appelle _analyze_orderflow_v6() et convertit le résultat en format
        compatible avec FusionManager.fuse().

        Returns:
            Dict compatible FusionManager:
            {
                "score": 0-100,  # Pourcentage pour FusionManager
                "status": "VALID"/"WEAK"/"SUSPECT",
                "summary": {
                    "delta_total": float,
                    "bias": "BUY"/"SELL"/"NEUTRAL",
                    "poc": float,
                    "imbalance_count": int,
                    "volume_ratio": float,
                    "mtf_alignment": dict
                },
                "total_score": 0-50,  # Score points pour rapport consolidé
                "details": dict  # Détails complets pour debugging
            }
        """
        # Appeler la fonction d'analyse existante
        result = self._analyze_orderflow_v6(
            asset=asset,
            df_m1=df_m1,
            df_m5=df_m5,
            df_m15=df_m15,
            asset_signals=asset_signals
        )

        # Extraire le score total (0-50 points)
        total_score = result.get("total_score", 0.0)

        # Convertir en pourcentage pour FusionManager (0-100)
        score_pct = (total_score / 50.0) * 100.0

        # Extraire détails pour construire summary
        details = result.get("details", {})
        delta_details = details.get("delta_momentum", {})
        volume_details = details.get("volume_confirmation", {})
        imbalance_details = details.get("imbalance_strength", {})

        # Construire summary pour FusionManager
        delta_total = delta_details.get("delta_total", 0.0)
        summary = {
            "delta_total": delta_total,
            "bias": "BUY" if delta_total > 0 else "SELL" if delta_total < 0 else "NEUTRAL",
            "poc": volume_details.get("poc"),
            "vpoc_price": volume_details.get("poc"),  # Alias pour compatibilité
            "imbalance_count": imbalance_details.get("m1_count", 0),
            "volume_ratio": volume_details.get("ratio", 1.0),
            "mtf_alignment": result.get("mtf_alignment", {}),
            "spike_detected": volume_details.get("spike_detected", False)
        }

        # Déterminer status selon qualité du score
        if total_score >= 30.0:  # 60% de 50 points
            status = "VALID"
        elif total_score >= 15.0:  # 30% de 50 points
            status = "WEAK"
        else:
            status = "SUSPECT"

        # Format final pour FusionManager
        return {
            "score": score_pct,  # 0-100 pour FusionManager._normalize_orderflow()
            "status": status,
            "summary": summary,
            "total_score": total_score,  # 0-50 pour rapport consolidé
            "details": details,  # Détails complets
            "raw_result": result  # Résultat brut si besoin
        }

    def _analyze_footprint_v6(
        self,
        asset: str,
        df_m1: pd.DataFrame,
        asset_signals: Dict[str, Any]
    ) -> Dict[str, Any]:
        """
        👣 Footprint Analysis V6 - Ticks Temps Réel

        Concentration sur bougie courante :
        • Analyse ticks en temps réel
        • Détection clusters d'ordres
        • Niveaux d'absorption critiques

        Contexte immédiat :
        • 3 bougies précédentes pour confirmation
        • Focus sur la bougie en cours (0-59 secondes)

        Retourne :
        {
            "absorption_levels_score": 0-15,
            "order_clustering_score": 0-10,
            "price_rejection_score": 0-5,
            "total_score": 0-30,
            "details": {...}
        }
        """
        result = {
            "absorption_levels_score": 0.0,
            "order_clustering_score": 0.0,
            "price_rejection_score": 0.0,
            "total_score": 0.0,
            "details": {}
        }

        try:
            # ✅ FIX (1er Décembre 2025): orchestrator stocke SEULEMENT "summary" (pas la structure complète)
            # Donc fp_raw contient directement {delta_total, buy_volume, ...} sans imbrication
            fp_raw = asset_signals.get("footprint_summary", {})

            # Vérifier si c'est une structure imbriquée (avec "summary") ou directe
            if isinstance(fp_raw, dict):
                if "summary" in fp_raw:
                    # Structure complète (depuis fusion_manager ou autre source)
                    fp_summary = fp_raw["summary"]
                else:
                    # Structure directe (depuis orchestrator)
                    fp_summary = fp_raw
            else:
                fp_summary = {}

            fp_status = asset_signals.get("footprint_status", "N/A").upper()


            # ================================================================
            # 1. ABSORPTION LEVELS (15 points max)
            # ================================================================
            absorption_score = 0.0
            absorption_details = {}

            # Récupérer les niveaux d'absorption depuis footprint
            buy_vol = 0
            sell_vol = 0

            # ✅ MÊME APPROCHE que fusion_manager.py (ligne 1008-1009) qui FONCTIONNAIT
            if isinstance(fp_summary, dict):
                buy_vol = float(fp_summary.get("buy_volume", 0))
                sell_vol = float(fp_summary.get("sell_volume", 0))

            total_vol = buy_vol + sell_vol

            # Stocker TOUJOURS les volumes (pour le rapport)
            absorption_details["buy_volume"] = buy_vol
            absorption_details["sell_volume"] = sell_vol

            if total_vol > 0:
                buy_ratio = buy_vol / total_vol
                sell_ratio = sell_vol / total_vol

                absorption_details["buy_ratio"] = buy_ratio
                absorption_details["sell_ratio"] = sell_ratio

                # Déterminer absorption
                if buy_ratio >= 0.75:  # 75%+ achats
                    absorption_score = 15.0
                    absorption_details["bias"] = "STRONG BULLISH"
                elif buy_ratio >= 0.65:
                    absorption_score = 12.0
                    absorption_details["bias"] = "BULLISH"
                elif sell_ratio >= 0.75:
                    absorption_score = 15.0
                    absorption_details["bias"] = "STRONG BEARISH"
                elif sell_ratio >= 0.65:
                    absorption_score = 12.0
                    absorption_details["bias"] = "BEARISH"
                else:
                    absorption_score = 5.0
                    absorption_details["bias"] = "NEUTRAL"

            # ✅ FIX (2 Décembre 2025): Log de debug pour comprendre le calcul
            self.logger.debug(
                f"[{asset}] Absorption: buy_vol={buy_vol:.1f} sell_vol={sell_vol:.1f} "
                f"total_vol={total_vol:.1f} buy_ratio={absorption_details.get('buy_ratio', 0):.2%} "
                f"bias={absorption_details.get('bias', 'N/A')} absorption_score={absorption_score:.1f}"
            )

            result["absorption_levels_score"] = absorption_score
            result["absorption_details"] = absorption_details  # FIX: Nom correct pour le rapport

            # ================================================================
            # 2. ORDER CLUSTERING (10 points max)
            # ================================================================
            clustering_score = 0.0
            clustering_details = {}

            # Analyser 3 bougies précédentes pour confirmation
            if df_m1 is not None and len(df_m1) >= 4:
                # Compter clusters (approximation via volume concentré)
                volumes = df_m1["tick_volume"].tail(4).values
                ranges = (df_m1["high"] - df_m1["low"]).tail(4).values

                # ✅ FIX (2 Décembre 2025): Calcul sécurisé évitant division par 0
                # Détect clusters : volume élevé + range faible = orders concentrés
                cluster_count = 0
                ranges_safe = np.maximum(ranges, 1e-9)  # Évite division by 0
                vol_per_pip_arr = volumes / ranges_safe
                median_vol_per_pip = np.median(vol_per_pip_arr)

                for i in range(len(volumes)):
                    if vol_per_pip_arr[i] > median_vol_per_pip:
                        cluster_count += 1

                clustering_details["cluster_count"] = cluster_count
                clustering_details["distribution"] = "concentrated" if cluster_count >= 2 else "dispersed"

                # Log de debug
                self.logger.debug(
                    f"[{asset}] Clustering: cluster_count={cluster_count} "
                    f"median_vol_per_pip={median_vol_per_pip:.2f} "
                    f"vol_per_pip={[f'{v:.1f}' for v in vol_per_pip_arr]}"
                )

                # Scoring
                if cluster_count >= 3:
                    clustering_score = 10.0
                elif cluster_count >= 2:
                    clustering_score = 7.0
                elif cluster_count >= 1:
                    clustering_score = 4.0
                else:
                    clustering_score = 0.0

            result["order_clustering_score"] = clustering_score
            result["clustering_details"] = clustering_details  # FIX: Nom correct pour le rapport

            # ================================================================
            # 3. PRICE REJECTION (5 points max)
            # ================================================================
            rejection_score = 0.0
            rejection_details = {}

            # Analyser wicks pour détecter rejets
            if df_m1 is not None and len(df_m1) >= 3:
                last_bars = df_m1.tail(3)

                rejection_count = 0
                for idx, row in last_bars.iterrows():
                    high_val = row["high"]
                    low_val = row["low"]
                    open_val = row["open"]
                    close_val = row["close"]

                    body = abs(close_val - open_val)
                    full_range = high_val - low_val

                    if full_range > 0:
                        upper_wick = high_val - max(open_val, close_val)
                        lower_wick = min(open_val, close_val) - low_val

                        wick_ratio = max(upper_wick, lower_wick) / body if body > 0 else 0

                        # Rejet net si wick > 2x body
                        if wick_ratio >= 2.0:
                            rejection_count += 1

                rejection_details["rejection_bars"] = rejection_count

                # Scoring
                if rejection_count >= 3:
                    rejection_score = 5.0
                    rejection_details["strength"] = "strong"
                elif rejection_count >= 2:
                    rejection_score = 3.0
                    rejection_details["strength"] = "moderate"
                elif rejection_count >= 1:
                    rejection_score = 2.0
                    rejection_details["strength"] = "weak"
                else:
                    rejection_score = 0.0
                    rejection_details["strength"] = "none"

            result["price_rejection_score"] = rejection_score
            result["rejection_details"] = rejection_details  # FIX: Nom correct pour le rapport

            # ================================================================
            # TOTAL FOOTPRINT SCORE
            # ================================================================
            result["total_score"] = (
                absorption_score +
                clustering_score +
                rejection_score
            )

            self.logger.debug(
                f"[{asset}] Footprint V6: Absorption={absorption_score:.1f} "
                f"Clustering={clustering_score:.1f} "
                f"Rejection={rejection_score:.1f} "
                f"→ Total={result['total_score']:.1f}/30"
            )

        except Exception as e:
            self.logger.error(f"[{asset}] Footprint V6 analysis error: {e}", exc_info=True)

        return result

    def _analyze_triggers_v6(
        self,
        asset: str,
        df_m1: pd.DataFrame,
        orderflow_result: Dict[str, Any],
        footprint_result: Dict[str, Any],
        asset_signals: Dict[str, Any]
    ) -> Dict[str, Any]:
        """
        ⚡ Triggers Detection V6 - Ultra-Rapide

        Détection ultra-rapide :
        • Bougie courante uniquement
        • Surveillance ticks temps réel
        • Seuils dynamiques selon volatilité

        Triggers disponibles :
        • Absorption @ POC : +8 points
        • Breakout imbalance : +6 points
        • Stop run bullish/bearish : +5 points
        • Volume spike : +4 points
        • Multi-trigger confluence : +2 à +4 points
        • Alignement MTF : +3 points

        Retourne :
        {
            "triggers": [...],
            "total_score": 0-20+ (peut dépasser 20),
            "details": {...}
        }
        """
        result = {
            "triggers": [],
            "total_score": 0.0,
            "details": {}
        }

        try:
            trigger_points = 0.0
            triggers_list = []

            # Récupérer POC depuis orderflow
            # ✅ FIX (2 Décembre 2025): Utiliser les vraies clés stockées
            orderflow_volume = orderflow_result.get("volume_confirmation_details", {})
            poc_price = orderflow_volume.get("poc")

            footprint_absorption = footprint_result.get("absorption_details", {})
            absorption_bias = footprint_absorption.get("bias", "NEUTRAL")

            # ================================================================
            # TRIGGER 1 : Absorption @ POC (+8 points)
            # ================================================================
            if poc_price and absorption_bias in ["STRONG BULLISH", "STRONG BEARISH"]:
                trigger_points += 8.0
                triggers_list.append({
                    "name": "absorption_at_poc",
                    "points": 8,
                    "direction": "bullish" if "BULLISH" in absorption_bias else "bearish"
                })
                self.logger.debug(f"[{asset}] ⚡ Trigger: Absorption @ POC (+8 pts)")

            # ================================================================
            # TRIGGER 2 : Breakout Imbalance (+6 points)
            # ================================================================
            # ✅ FIX (2 Décembre 2025): Utiliser la vraie clé
            orderflow_imbalances = orderflow_result.get("imbalance_strength_details", {})
            imbalance_count = orderflow_imbalances.get("total_count", 0)

            if imbalance_count >= 3:  # Imbalances significatives
                trigger_points += 6.0
                triggers_list.append({
                    "name": "breakout_imbalance",
                    "points": 6,
                    "imbalances": imbalance_count
                })
                self.logger.debug(f"[{asset}] ⚡ Trigger: Breakout Imbalance (+6 pts)")

            # ================================================================
            # TRIGGER 3 : Volume Spike (+4 points)
            # ================================================================
            volume_spike = orderflow_volume.get("spike_detected", False)

            if volume_spike:
                trigger_points += 4.0
                triggers_list.append({
                    "name": "volume_spike",
                    "points": 4,
                    "ratio": orderflow_volume.get("ratio", 0)
                })
                self.logger.debug(f"[{asset}] ⚡ Trigger: Volume Spike (+4 pts)")

            # ================================================================
            # TRIGGER 4 : Stop Run (+5 points) - Détecté via rejection
            # ================================================================
            # ✅ FIX (2 Décembre 2025): Utiliser la vraie clé
            rejection_strength = footprint_result.get("rejection_details", {}).get("strength")

            if rejection_strength == "strong":
                trigger_points += 5.0
                triggers_list.append({
                    "name": "stop_run",
                    "points": 5,
                    "strength": "strong"
                })
                self.logger.debug(f"[{asset}] ⚡ Trigger: Stop Run (+5 pts)")

            # ================================================================
            # ✅ NOUVEAUX TRIGGERS (2 Décembre 2025) - Moins Restrictifs
            # ================================================================

            # TRIGGER 5 : Delta Strong (+3 points)
            # ================================================================
            orderflow_delta_details = orderflow_result.get("delta_momentum_details", {})
            delta_total = abs(orderflow_delta_details.get("delta_total", 0))

            if delta_total >= 100:
                trigger_points += 3.0
                triggers_list.append({
                    "name": "delta_strong",
                    "points": 3,
                    "delta": delta_total
                })
                self.logger.debug(f"[{asset}] ⚡ Trigger: Delta Strong (+3 pts) | delta={delta_total:.1f}")

            # ================================================================
            # TRIGGER 6 : High Imbalance Ratio (+2 points)
            # ================================================================
            imbalance_total = orderflow_imbalances.get("total_count", 0)

            if imbalance_total >= 50:
                trigger_points += 2.0
                triggers_list.append({
                    "name": "high_imbalance_ratio",
                    "points": 2,
                    "count": imbalance_total
                })
                self.logger.debug(f"[{asset}] ⚡ Trigger: High Imbalance Ratio (+2 pts) | count={imbalance_total}")

            # ================================================================
            # TRIGGER 7 : Partial MTF Coherence (+2 points)
            # ================================================================
            mtf_alignment = orderflow_result.get("mtf_alignment", {})
            m1_dir = mtf_alignment.get("m1", "NEUTRAL").upper()
            m5_dir = mtf_alignment.get("m5", "NEUTRAL").upper()
            m15_dir = mtf_alignment.get("m15", "NEUTRAL").upper()

            # Compter alignements (2/3 suffisent)
            directions = [m1_dir, m5_dir, m15_dir]
            bullish_count = directions.count("BULLISH")
            bearish_count = directions.count("BEARISH")

            if bullish_count >= 2 or bearish_count >= 2:
                trigger_points += 2.0
                triggers_list.append({
                    "name": "partial_mtf_coherence",
                    "points": 2,
                    "alignment": f"{max(bullish_count, bearish_count)}/3"
                })
                self.logger.debug(f"[{asset}] ⚡ Trigger: Partial MTF Coherence (+2 pts) | {max(bullish_count, bearish_count)}/3 aligned")

            # ================================================================
            # BONUS : Multi-Trigger Confluence (+2 à +4 points)
            # ================================================================
            if len(triggers_list) >= 3:
                bonus = 4
            elif len(triggers_list) >= 2:
                bonus = 2
            else:
                bonus = 0

            if bonus > 0:
                trigger_points += bonus
                triggers_list.append({
                    "name": "multi_trigger_confluence",
                    "points": bonus,
                    "trigger_count": len(triggers_list)
                })
                self.logger.debug(f"[{asset}] ⚡ Bonus: Multi-Trigger Confluence (+{bonus} pts)")

            # ================================================================
            # BONUS : Alignement Multi-Timeframe (+3 points)
            # ================================================================
            mtf_aligned = orderflow_result.get("mtf_aligned", False)

            if mtf_aligned:
                trigger_points += 3.0
                triggers_list.append({
                    "name": "mtf_alignment",
                    "points": 3,
                    "alignment": orderflow_result.get("mtf_alignment")
                })
                self.logger.debug(f"[{asset}] ⚡ Bonus: MTF Alignment (+3 pts)")

            # ================================================================
            # TOTAL TRIGGERS SCORE
            # ================================================================
            result["triggers"] = triggers_list
            result["total_score"] = trigger_points
            result["details"]["trigger_count"] = len(triggers_list)

            self.logger.debug(
                f"[{asset}] Triggers V6: {len(triggers_list)} triggers → {trigger_points:.1f} points"
            )

        except Exception as e:
            self.logger.error(f"[{asset}] Triggers V6 analysis error: {e}", exc_info=True)

        return result

    def _log_orderflow_consolidated_report(
        self,
        asset: str,
        orderflow_result: Dict[str, Any],
        footprint_result: Dict[str, Any],
        triggers_result: Dict[str, Any],
        final_score: float,
        action: Optional[str]
    ) -> None:
        """
        📋 RAPPORT CONSOLIDÉ ORDERFLOW V6 - BURST SCALPING

        Affiche un bilan formaté des trois composants et du score final
        """
        try:
            sep = "=" * 70

            self.logger.info(f"\n{sep}")
            self.logger.info(f"📊 ORDERFLOW V6 - ANALYSE BURST SCALPING [{asset}]")
            self.logger.info(f"{sep}")

            # ================================================================
            # 1. ANALYSE MULTI-TIMEFRAME
            # ================================================================
            mtf_alignment = orderflow_result.get("mtf_alignment", {})
            m1_dir = mtf_alignment.get("m1", "N/A")
            m5_dir = mtf_alignment.get("m5", "N/A")
            m15_dir = mtf_alignment.get("m15", "N/A")
            mtf_aligned = orderflow_result.get("mtf_aligned", False)

            self.logger.info(f"\n⏱️  PÉRIODES MULTI-TIMEFRAME :")
            self.logger.info(f"   • M1  (8 bougies)  → Momentum : {m1_dir.upper()}")
            self.logger.info(f"   • M5  (6 bougies)  → Structure : {m5_dir.upper()}")
            self.logger.info(f"   • M15 (4 bougies)  → Contexte  : {m15_dir.upper()}")

            if mtf_aligned:
                self.logger.info(f"   ✅ ALIGNEMENT MTF DÉTECTÉ (+3 pts bonus)")
            else:
                self.logger.info(f"   ⚠️  Pas d'alignement multi-timeframe")

            # ================================================================
            # 2. ORDERFLOW ANALYSIS (50% du score)
            # ================================================================
            of_score = orderflow_result.get("total_score", 0.0)
            delta_score = orderflow_result.get("delta_momentum_score", 0.0)
            volume_score = orderflow_result.get("volume_confirmation_score", 0.0)
            imbalance_score = orderflow_result.get("imbalance_strength_score", 0.0)

            delta_details = orderflow_result.get("delta_momentum_details", {})
            volume_details = orderflow_result.get("volume_confirmation_details", {})
            imbalance_details = orderflow_result.get("imbalance_strength_details", {})

            self.logger.info(f"\n📈 ORDERFLOW ANALYSIS (50% du total) : {of_score:.1f}/50 points")
            self.logger.info(f"   ├─ Delta Momentum      : {delta_score:.1f}/25 pts")
            self.logger.info(f"   │  • Delta total       : {delta_details.get('delta_total', 0)}")
            self.logger.info(f"   │  • Cohérence         : {delta_details.get('coherence', 0)*100:.0f}%")
            self.logger.info(f"   │  • Direction         : {delta_details.get('delta_direction', 'N/A').upper()}")

            self.logger.info(f"   ├─ Volume Confirmation : {volume_score:.1f}/15 pts")
            self.logger.info(f"   │  • Volume ratio      : {volume_details.get('ratio', 0):.2f}x")
            self.logger.info(f"   │  • Spike détecté     : {'OUI' if volume_details.get('spike_detected') else 'NON'}")
            poc_val = volume_details.get('poc')
            poc_str = f"{poc_val:.2f}" if poc_val is not None else "N/A"
            self.logger.info(f"   │  • POC (Point of Control) : {poc_str}")

            self.logger.info(f"   └─ Imbalance Strength  : {imbalance_score:.1f}/10 pts")
            m1_count = imbalance_details.get("m1_count", 0)
            m5_count = imbalance_details.get("m5_count", 0)
            self.logger.info(f"      • Imbalances M1    : {m1_count} détectées")
            self.logger.info(f"      • Imbalances M5    : {m5_count} détectées")

            # ================================================================
            # 3. FOOTPRINT ANALYSIS (30% du score)
            # ================================================================
            fp_score = footprint_result.get("total_score", 0.0)
            # ✅ FIX (2 Décembre 2025): Corriger clés pour matcher les vraies clés stockées
            absorption_score = footprint_result.get("absorption_levels_score", 0.0)
            clustering_score = footprint_result.get("order_clustering_score", 0.0)
            rejection_score = footprint_result.get("price_rejection_score", 0.0)

            absorption_details = footprint_result.get("absorption_details", {})
            clustering_details = footprint_result.get("clustering_details", {})
            rejection_details = footprint_result.get("rejection_details", {})

            self.logger.info(f"\n👣 FOOTPRINT ANALYSIS (30% du total) : {fp_score:.1f}/30 points")
            self.logger.info(f"   ├─ Absorption Levels   : {absorption_score:.1f}/15 pts")
            self.logger.info(f"   │  • Biais absorption  : {absorption_details.get('bias', 'N/A')}")
            self.logger.info(f"   │  • Buy ratio         : {absorption_details.get('buy_ratio', 0)*100:.0f}%")
            self.logger.info(f"   │  • Sell ratio        : {absorption_details.get('sell_ratio', 0)*100:.0f}%")

            self.logger.info(f"   ├─ Order Clustering    : {clustering_score:.1f}/10 pts")
            self.logger.info(f"   │  • Clusters détectés : {clustering_details.get('cluster_count', 0)}")
            # ✅ FIX (2 Décembre 2025): Utiliser "distribution" (clé correcte)
            self.logger.info(f"   │  • Distribution      : {clustering_details.get('distribution', 'N/A')}")

            self.logger.info(f"   └─ Price Rejection     : {rejection_score:.1f}/5 pts")
            # ✅ FIX (2 Décembre 2025): Utiliser "rejection_bars" et "strength" (clés correctes)
            self.logger.info(f"      • Rejets détectés   : {rejection_details.get('rejection_bars', 0)}/3")
            self.logger.info(f"      • Force rejet       : {rejection_details.get('strength', 'N/A')}")

            # ================================================================
            # 4. TRIGGERS DETECTION (20% du score)
            # ================================================================
            trig_score = triggers_result.get("total_score", 0.0)
            trigger_points = triggers_result.get("trigger_points", 0.0)
            triggers_list = triggers_result.get("triggers_detected", [])

            self.logger.info(f"\n⚡ TRIGGERS DETECTION (20% du total) : {trig_score:.1f}/20 points")

            if triggers_list:
                self.logger.info(f"   Triggers détectés ({len(triggers_list)}) :")
                for trig in triggers_list:
                    name = trig.get("name", "unknown")
                    pts = trig.get("points", 0)
                    direction = trig.get("direction", "N/A")

                    # Emoji selon le type
                    emoji = "🎯"
                    if "absorption" in name:
                        emoji = "🔵"
                    elif "breakout" in name or "imbalance" in name:
                        emoji = "🔓"
                    elif "volume" in name:
                        emoji = "📊"
                    elif "stop_run" in name:
                        emoji = "🎣"

                    self.logger.info(f"   {emoji} {name.replace('_', ' ').title()} → +{pts} pts ({direction})")
            else:
                self.logger.info(f"   ⚠️  Aucun trigger détecté")

            # Bonus
            bonus_mtf = triggers_result.get("bonus_mtf_alignment", 0)
            bonus_confluence = triggers_result.get("bonus_multi_trigger_confluence", 0)

            if bonus_mtf > 0:
                self.logger.info(f"   ✨ Bonus MTF Alignment : +{bonus_mtf} pts")
            if bonus_confluence > 0:
                self.logger.info(f"   ✨ Bonus Multi-Trigger : +{bonus_confluence} pts")

            # ================================================================
            # 5. SCORE FINAL & DÉCISION
            # ================================================================
            self.logger.info(f"\n{sep}")
            self.logger.info(f"🎯 SCORE FINAL ORDERFLOW V6")
            self.logger.info(f"{sep}")
            # ✅ FIX (2 Décembre 2025): Scores déjà pondérés, pas de multiplication
            self.logger.info(f"   OrderFlow (50%) : {of_score:.1f}/50 pts")
            self.logger.info(f"   Footprint (30%) : {fp_score:.1f}/30 pts")
            self.logger.info(f"   Triggers  (20%) : {trig_score:.1f}/20 pts")
            self.logger.info(f"   {'─' * 50}")
            self.logger.info(f"   TOTAL           : {final_score:.1f}/100 points")

            # Direction recommandée
            if action:
                action_emoji = "🟢" if action == "BUY" else "🔴"
                self.logger.info(f"\n   {action_emoji} Direction recommandée : {action}")

            self.logger.info(f"{sep}\n")

        except Exception as e:
            self.logger.error(f"[{asset}] Erreur rapport consolidé OrderFlow V6: {e}", exc_info=True)


    def evaluate_entry(
        self,
        asset: str,
        analyzed_context: Dict[str, Any],
        asset_signals: Dict[str, Any],
    ) -> Dict[str, Any]:
        """
        Version 'desk pro' compatible pipeline:
        - Pas de fallback implicite → uniquement des règles explicites
        - Priorité:
            1. Marubozu (MarketAnalyzer + Playbook)
            2. Range Accumulation MTF
            3. Range Accumulation simple
            4. Burst single_master
        - ATR/Spread n'affecte que le burst single_master si des seuils sont configurés
        """
        try:
            # --- 0) Données & config ---
            ctx_md = (analyzed_context.get("market_data") or {}).get(asset, {}) or {}
            
            # ✅ Résolution sûre (pas d'évaluation booléenne de DataFrame)
            df_m1 = None
            for __k in ("annotated_rates_df_m1", "annotated_rates_df", "df_m1", "df"):
                __v = ctx_md.get(__k)
                if isinstance(__v, pd.DataFrame):
                    df_m1 = __v
                    break

            df_work = (
                df_m1.copy()
                if isinstance(df_m1, pd.DataFrame) and len(df_m1) >= 50
                else None
            )
            # --- 0b) Action hint (BUY/SELL) par défaut ---
            action = None

            def _norm_dir(x):
                if not x:
                    return None
                x = str(x).upper()
                if x in ("BUY", "LONG", "BULL", "BULLISH"):
                    return "BUY"
                if x in ("SELL", "SHORT", "BEAR", "BEARISH"):
                    return "SELL"
                return None

            # --- 0a) Config stratégie (pour meta & règles) ---
            try:
                strat_cfg = self.config_manager.get_strategy_config("scalping") or {}
            except Exception:
                strat_cfg = self.strategy_config or {}

            # --- 0c) Intégration footprint ---
            try:
                fp_score = float(asset_signals.get("footprint_score", 0.0))
                fp_status = str(asset_signals.get("footprint_status", "N/A")).upper()
                fp_summary = asset_signals.get("footprint_summary", {})

                # Boost de confiance si footprint cohérent avec le biais
                if (
                    fp_status == "BULLISH"
                    and fp_score > 0
                    and asset_signals.get("phase", "").lower().startswith("bull")
                ):
                    asset_signals["confidence_score"] = min(
                        1.0, float(asset_signals.get("confidence_score", 0.5)) + 0.15
                    )
                    self.logger.info(
                        f"[{asset}] 📊 Footprint bullish → confiance renforcée"
                    )

                if (
                    fp_status == "BEARISH"
                    and fp_score > 0
                    and asset_signals.get("phase", "").lower().startswith("bear")
                ):
                    asset_signals["confidence_score"] = min(
                        1.0, float(asset_signals.get("confidence_score", 0.5)) + 0.15
                    )
                    self.logger.info(
                        f"[{asset}] 📊 Footprint bearish → confiance renforcée"
                    )

                # Early entry si déséquilibre extrême (optionnel)
                try:
                    delta = float(fp_summary.get("delta_total", 0)) if isinstance(fp_summary, dict) else 0
                except Exception:
                    delta = None
                if isinstance(delta, (int, float)) and abs(delta) >= 300:
                    asset_signals["early_entry_allowed"] = True
                    self.logger.info(
                        f"[{asset}] ⚡ Early entry activé (Δ={delta}) via footprint"
                    )
                else:
                    asset_signals["early_entry_allowed"] = False
                    
                # --- 0d) Déduction robuste de l'action ---
                # 1) indices directs depuis les signaux
                action = (
                    _norm_dir(asset_signals.get("action"))
                    or _norm_dir(asset_signals.get("bias"))
                    or _norm_dir(asset_signals.get("direction"))
                    or action
                )

                # 2) fallback via la phase quand aucun indice direct
                if action is None:
                    ph = str(asset_signals.get("phase", "")).lower()
                    if ph.startswith("trend_bull") or ph.startswith("breakout_bull"):
                        action = "BUY"
                    elif ph.startswith("trend_bear") or ph.startswith("breakout_bear"):
                        action = "SELL"

                # 3) dernier filet via le footprint (si résumé dispo)
                try:
                    if action is None and isinstance(fp_summary, dict):
                        d = float(fp_summary.get("delta_total", 0))
                        if d > 0:
                            action = "BUY"
                        elif d < 0:
                            action = "SELL"
                except Exception:
                    pass
    
            except Exception as e:
                self.logger.warning(f"[{asset}] Footprint integration skipped: {e}")
           
            # --- 1) Métadonnées (pip_size, spread, etc.) ---
            meta = self._safe_asset_meta(asset, asset_signals, analyzed_context, strat_cfg)
            pip_size = meta["pip_size"]
            if pip_size <= 0:
                self.logger.warning(f"[{asset}] pip_size invalide.")
                return {}
           
            # --- 2) Prix courant ---
            price = self._safe_price_from_signals(asset_signals)
            if not price:
                self.logger.info(f"[{asset}] Pas de prix exploitable dans les signaux.")
                return {}                  

            # --- 5) Range Accumulation MTF ---
            try:
                mtf_cfg = strat_cfg.get("range_accumulation_mtf") or {}
                if mtf_cfg.get("enabled", False):  # ✅ Vérifier enabled
                    mtf_decision = self._rule_range_accumulation_mtf(
                        df_m1=df_work,
                        asset=asset,
                        price=price,
                        meta=meta,
                        cfg=mtf_cfg,
                        analyzed_context=analyzed_context,
                    )
                    if mtf_decision:
                        return self._finalize_decision(mtf_decision, analyzed_context)
                
                          # --- 3) Momentum & Breakout (nouvelles règles) ---
                try:
                    mom_cfg = (strat_cfg.get("momentum") or {}) if isinstance(strat_cfg, dict) else {}
                    pat_cfg = (strat_cfg.get("patterns") or {}) if isinstance(strat_cfg, dict) else {}

                    # ✅ Vérifier que momentum ET patterns sont enabled
                    if not mom_cfg.get("enabled", False) and not pat_cfg.get("enabled", False):
                        self.logger.debug(f"[{asset}] Momentum/Pattern désactivés dans config")
                    else:
                        weights = (strat_cfg.get("scoring_weights") or {"context": 0.3, "technical": 0.4, "orderflow": 0.2, "risk": 0.1})
                        thresholds = (strat_cfg.get("scoring_thresholds") or {"direct": 0.70, "conditional": 0.50})

                        candidates: List[Dict[str, Any]] = []

                        # Evaluer momentum patterns seulement si enabled
                        if mom_cfg.get("enabled", False):
                            rb = self._rule_breakout_consolidation(df_work, asset, price, meta, mom_cfg.get("breakout", {}) or {})
                            if rb: candidates.append(rb)

                            tpull = self._rule_trend_pullback(df_work, asset, price, meta, mom_cfg.get("trend_pullback", {}) or {})
                            if tpull: candidates.append(tpull)

                            ign = self._rule_momentum_ignition(df_work, asset, price, meta, mom_cfg.get("ignition", {}) or {})
                            if ign: candidates.append(ign)

                        # Evaluer patterns seulement si enabled
                        if pat_cfg.get("enabled", False):
                            ib = self._rule_inside_bar_breakout(df_work, asset, price, meta, (pat_cfg.get("inside_bar", {}) or {}))
                            if ib: candidates.append(ib)

                        if candidates:
                            best = self._choose_best_candidate(
                                candidates=candidates,
                                asset=asset,
                                meta=meta,
                                asset_signals=asset_signals,
                                analyzed_context=analyzed_context,
                                weights=weights,
                                thresholds=thresholds,
                            )
                            if best and float(best.get("score", 0.0)) >= float(thresholds.get("direct", 0.70)):
                                return self._finalize_decision(best, analyzed_context)
                except Exception as e:
                    self.logger.debug(f"[{asset}] Momentum/Pattern block skipped: {e}")

            except Exception as e:
                self.logger.debug(f"[{asset}] MTF range-accum skipped: {e}")

            # --- 6) Range Accumulation simple ---
            try:
                range_cfg = strat_cfg.get("range_accumulation") or {}
                if range_cfg.get("enabled", False):  # ✅ Vérifier enabled
                    range_decision = self._rule_range_accumulation(
                        df=df_work,
                        asset=asset,
                        price=price,
                        action=action,
                        meta=meta,
                        cfg=range_cfg,
                    )
                    if range_decision:
                        range_decision.setdefault("strategy_type", "scalping")
                        range_decision.setdefault("rule_name", "range_accumulation")
                        range_decision.setdefault("execution_status", "ready")
                        return self._finalize_decision(range_decision, analyzed_context)
            except Exception as e:
                self.logger.debug(f"[{asset}] Range accumulation simple skipped: {e}")

            # --- 7) Burst single_master ---
            # ATR M1 (optionnel, pour guardrails si tu configures un seuil)
            atr_m1_pips = None
            if isinstance(df_work, pd.DataFrame):
                atr_m1 = self._atr(df_work, period=14)
                atr_m1_pips = (
                    (atr_m1 / pip_size)
                    if isinstance(atr_m1, (int, float)) and atr_m1 > 0 and pip_size > 0
                    else None
                )

            # Config guardrails (seuils globaux si présents)
            guardrails_cfg = {}
            try:
                guardrails_cfg = self.config_manager.get("guardrails", {}) or {}
            except Exception:
                guardrails_cfg = getattr(self.config_manager, "guardrails", {}) or {}

            # Config burst_scalping
            sm_cfg = ((strat_cfg.get("entry_rules") or {}).get("scalping") or {}).get(
                "burst_scalping", {}
            ) or {}
            if not bool(sm_cfg.get("enabled", True)):
                self.logger.info(f"[{asset}] single_master désactivé en config.")
                self.logger.info(
                    f"[DEBUG][{asset}] evaluate_entry terminé → AUCUN setup retenu."
                )
                return {}

            # Seuils dynamiques (optionnels)
            try:
                max_spread_sm = float(
                    sm_cfg.get(
                        "max_spread_pips",
                        (guardrails_cfg.get("volatility", {}) or {}).get(
                            "max_spread_pips", 0.0
                        ),
                    )
                    or 0.0
                )
            except Exception:
                max_spread_sm = 0.0

            try:
                min_atr_req = float(
                    sm_cfg.get(
                        "min_atr_m1_pips",
                        (guardrails_cfg.get("volatility", {}) or {}).get(
                            "min_atr_m1_pips", 0.0
                        ),
                    )
                    or 0.0
                )
            except Exception:
                min_atr_req = 0.0

            # Gating simple selon les seuils si fournis
            if max_spread_sm > 0 and meta.get("spread_pips", 0.0) > max_spread_sm:
                self.logger.info(
                    f"[{asset}] REFUS single_master → spread {meta['spread_pips']:.2f}p > seuil {max_spread_sm:.2f}p"
                )
                self.logger.info(
                    f"[DEBUG][{asset}] evaluate_entry terminé → AUCUN setup retenu."
                )
                return {}

            if min_atr_req > 0.0 and (atr_m1_pips is None or atr_m1_pips < min_atr_req):
                self.logger.info(
                    f"[{asset}] REFUS single_master → ATR M1 {atr_m1_pips or 0:.2f}p < seuil {min_atr_req:.2f}p"
                )
                self.logger.info(
                    f"[DEBUG][{asset}] evaluate_entry terminé → AUCUN setup retenu."
                )
                return {}

            # ================================================================
            # ORDERFLOW V6 - ANALYSE MULTI-COMPOSANTS (M1/M5/M15)
            # ================================================================
            try:
                # Récupération des DataFrames multi-timeframe
                df_m5 = None
                df_m15 = None

                # ✅ DEBUG: Log structure analyzed_context
                ctx_md = (analyzed_context.get("market_data") or {}).get(asset, {}) or {}
                self.logger.debug(f"[OF V6][{asset}] market_data keys: {list(ctx_md.keys())}")

                # Essayer de récupérer M5 depuis analyzed_context
                try:
                    for k in ("annotated_rates_df_m5", "df_m5", "rates_m5"):
                        v = ctx_md.get(k)
                        if isinstance(v, pd.DataFrame) and len(v) >= 4:
                            df_m5 = v
                            self.logger.debug(f"[OF V6][{asset}] M5 trouvé via clé '{k}' | len={len(df_m5)}")
                            break
                except Exception as e:
                    self.logger.debug(f"[OF V6][{asset}] Erreur récupération M5: {e}")

                # Essayer de récupérer M15 depuis analyzed_context
                try:
                    for k in ("annotated_rates_df_m15", "df_m15", "rates_m15"):
                        v = ctx_md.get(k)
                        if isinstance(v, pd.DataFrame) and len(v) >= 4:
                            df_m15 = v
                            self.logger.debug(f"[OF V6][{asset}] M15 trouvé via clé '{k}' | len={len(df_m15)}")
                            break
                except Exception as e:
                    self.logger.debug(f"[OF V6][{asset}] Erreur récupération M15: {e}")

                # Si M5/M15 non trouvés, essayer de les récupérer via MT5
                if df_m5 is None and self.mt5_connector:
                    try:
                        import MetaTrader5 as mt5
                        df_m5 = self.mt5_connector.get_rates(asset, mt5.TIMEFRAME_M5, count=20)
                        if df_m5 is not None and len(df_m5) >= 4:
                            self.logger.debug(f"[OF V6][{asset}] M5 récupéré via MT5 | len={len(df_m5)}")
                    except Exception as e:
                        self.logger.debug(f"[OF V6][{asset}] Impossible récupérer M5 via MT5: {e}")

                if df_m15 is None and self.mt5_connector:
                    try:
                        import MetaTrader5 as mt5
                        df_m15 = self.mt5_connector.get_rates(asset, mt5.TIMEFRAME_M15, count=15)
                        if df_m15 is not None and len(df_m15) >= 4:
                            self.logger.debug(f"[OF V6][{asset}] M15 récupéré via MT5 | len={len(df_m15)}")
                    except Exception as e:
                        self.logger.debug(f"[OF V6][{asset}] Impossible récupérer M15 via MT5: {e}")

                # ⚡ 1. OrderFlow Analysis (50% du score)
                orderflow_result = self._analyze_orderflow_v6(
                    asset=asset,
                    df_m1=df_work,
                    df_m5=df_m5,
                    df_m15=df_m15,
                    asset_signals=asset_signals
                )

                # 👣 2. Footprint Analysis (30% du score)
                footprint_result = self._analyze_footprint_v6(
                    asset=asset,
                    df_m1=df_work,
                    asset_signals=asset_signals
                )

                # 🎯 3. Triggers Detection (20% du score)
                triggers_result = self._analyze_triggers_v6(
                    asset=asset,
                    df_m1=df_work,
                    orderflow_result=orderflow_result,
                    footprint_result=footprint_result,
                    asset_signals=asset_signals
                )

                # 📊 4. SCORING FINAL PONDÉRÉ
                orderflow_score = orderflow_result.get("total_score", 0.0)
                footprint_score = footprint_result.get("total_score", 0.0)
                triggers_score = triggers_result.get("total_score", 0.0)

                # ✅ FIX (2 Décembre 2025): Les scores sont DÉJÀ pondérés (/50, /30, /20)
                # Pas besoin de multiplier à nouveau, il suffit d'additionner !
                # AVANT (FAUX): 20×0.5 + 14×0.3 + 10×0.2 = 16.2/100
                # APRÈS (CORRECT): 20 + 14 + 10 = 44/100
                final_score = orderflow_score + footprint_score + triggers_score

                # 📋 5. RAPPORT CONSOLIDÉ
                self._log_orderflow_consolidated_report(
                    asset=asset,
                    orderflow_result=orderflow_result,
                    footprint_result=footprint_result,
                    triggers_result=triggers_result,
                    final_score=final_score,
                    action=action
                )

                # ✅ AUCUN SEUIL ICI - FusionManager gère TOUT avec scoring_thresholds
                # (high: 0.80, moderate: 0.75, cautious: 0.70, conditional: 0.40)

            except Exception as e:
                self.logger.warning(f"[{asset}] OrderFlow V6 analysis failed: {e}", exc_info=True)
                # Continuer sans OrderFlow V6 si erreur (fallback)

            # ================================================================
            # DÉCISION BURST SCALPING
            # ================================================================
            # Décision single_master (aucun volume/SL ici → calculés en aval dans prepare_order)
            entry_mode = str(sm_cfg.get("entry_mode", "MARKET")).upper()
            burst_sz = int(sm_cfg.get("burst_size", 5) or 5)

            sm_decision = {
                "strategy_type": "scalping",
                "rule_name": "burst_scalping",
                "execution_status": "ready",
                "action": action,
                "asset": asset,
                "order_type": entry_mode,  # MARKET / BUY_LIMIT / SELL_LIMIT
                "entry_price": (float(price) if entry_mode != "MARKET" else None),
                "burst_size": burst_sz,  # virtuel (logique interne)
                "meta": {
                    "burst": True,
                    "entry_source": "core_decision",
                    "per_leg_virtual": bool(sm_cfg.get("per_leg_virtual", True)),
                    "atr_m1_pips": atr_m1_pips,
                    "orderflow_v6_score": final_score if 'final_score' in locals() else None,
                },
            }
            return self._finalize_decision(sm_decision, analyzed_context)

            # --- Aucun setup valide ---
            # (Jamais atteint car on retourne au-dessus pour SM ; gardé par sécurité)
            self.logger.info(
                f"[DEBUG][{asset}] evaluate_entry terminé → AUCUN setup retenu."
            )
            return {}

        except Exception as e:
            self.logger.error(f"[{asset}] evaluate_entry error: {e}", exc_info=True)
            return {}

    # ==========================================================
    # =============       RÈGLES D’ENTRÉE       ================
    # ==========================================================
    # === [SCORING] Multi-critères Contexte/Technique/OrderFlow/Risque ========
    def _choose_best_candidate(
        self,
        candidates: List[Dict[str, Any]],
        asset: str,
        meta: Dict[str, Any],
        asset_signals: Dict[str, Any],
        analyzed_context: Dict[str, Any],
        weights: Dict[str, float],
        thresholds: Dict[str, float],
    ) -> Optional[Dict[str, Any]]:
        best = None
        best_score = -1.0
        for c in candidates:
            s = self._score_candidate(c, meta, asset_signals, analyzed_context, weights)
            c["score"] = float(max(0.0, min(1.0, s)))
            if c["score"] > best_score:
                best, best_score = c, c["score"]
        return best

    def _score_candidate(
        self,
        c: Dict[str, Any],
        meta: Dict[str, Any],
        asset_signals: Dict[str, Any],
        analyzed_context: Dict[str, Any],
        weights: Dict[str, float],
    ) -> float:
        # --- 1) Contexte (phase/MTF & confiance globale signaux) ---
        phase = str(asset_signals.get("phase", "") or "").lower()
        conf = float(asset_signals.get("confidence_score", 0.5) or 0.5)
        align = 0.5
        if c.get("action") == "BUY" and any(k in phase for k in ("bull", "up", "accum", "trend")):
            align = 1.0
        if c.get("action") == "SELL" and any(k in phase for k in ("bear", "down", "distrib", "trend")):
            align = 1.0 if "trend" in phase or "bear" in phase else 0.8
        context_score = 0.5 * conf + 0.5 * align
        context_score = max(0.0, min(1.0, context_score))

        # --- 2) Technique (score interne du setup) ---
        tech = c.get("technical_score")
        if tech is None:
            tech = c.get("confidence", 0.6)
        technical_score = max(0.0, min(1.0, float(tech)))

        # --- 3) Order Flow (footprint/delta si dispo) ---
        of = 0.0
        try:
            fp = float(asset_signals.get("footprint_score", 0.0) or 0.0) / 100.0
            of = max(of, fp)
            d = (asset_signals.get("footprint_summary") or {}).get("delta_total")
            if isinstance(d, (int, float)):
                of = max(of, min(abs(d) / 500.0, 1.0) * 0.7)  # plafonné
        except Exception:
            pass
        orderflow_score = max(0.0, min(1.0, of))

        # --- 4) Risque (spread bas = mieux ; heuristique simple) ---
        sp = float(meta.get("spread_pips", 0.0) or 0.0)
        if sp <= 5:
            risk_score = 1.0
        elif sp <= 10:
            risk_score = 0.8
        elif sp <= 15:
            risk_score = 0.6
        else:
            risk_score = 0.3

        w = lambda k, d: float(weights.get(k, d))
        score = (
            w("context", 0.3) * context_score
            + w("technical", 0.4) * technical_score
            + w("orderflow", 0.2) * orderflow_score
            + w("risk", 0.1) * risk_score
        )
        return float(score)

    # === [PATTERNS MOMENTUM SUPPRIMÉS - Session 23 Nov 2025] ===
    # Les fonctions suivantes ont été supprimées car désactivées dans la config :
    # - _rule_breakout_consolidation (45 lignes)
    # - _rule_trend_pullback (47 lignes)
    # - _rule_inside_bar_breakout (40 lignes)
    # - _rule_momentum_ignition (106 lignes)
    # Total supprimé : ~180 lignes de code mort


    def _get_atr_m1_pips(
        self,
        asset: str,
        signals: Dict[str, Any],
        context: Dict[str, Any],
        pip_size_value: float,
    ) -> Optional[float]:
        """
        Essaie de récupérer et convertir l'ATR M1 en pips à partir de plusieurs sources.
        Retourne None si toutes les conversions échouent.
        """
        atr_sources: List[Tuple[str, Any, bool]] = [
            ("signals['atr_m1_pips']", signals.get("atr_m1_pips"), False),
            (
                "context['atr_m1_pips']",
                (
                    (context or {}).get("atr_m1_pips")
                    if isinstance(context, dict)
                    else None
                ),
                False,
            ),
            ("signals['atr_m1']", signals.get("atr_m1"), True),
        ]

        conversion_errors: List[str] = []

        for label, raw_value, needs_division in atr_sources:
            try:
                numeric_value = float(raw_value)
            except (TypeError, ValueError) as exc:
                conversion_errors.append(f"{label}: {exc}")
                continue

            if needs_division:
                if pip_size_value <= 0:
                    conversion_errors.append(
                        f"{label}: pip_size {pip_size_value} invalide pour conversion"
                    )
                    continue
                candidate = numeric_value / pip_size_value
            else:
                candidate = numeric_value

            if candidate > 0:
                return candidate
            conversion_errors.append(
                f"{label}: valeur <= 0 après conversion ({candidate})"
            )

        if conversion_errors:
            self.logger.debug(
                f"[{asset}] Burst scalping: conversions ATR M1 pips échouées ({'; '.join(conversion_errors)})"
            )
        return None
  

    # =====================================================
    # Tes règles scalping/liquidity existantes commencent ici
    # =====================================================

    def _rule_burst_scalping(
        self,
        asset: str,
        action: str,
        entry_price: float,
        meta: Dict[str, Any],
        sm_cfg: Dict[str, Any],
        context: Dict[str, Any],
    ) -> Optional[Dict[str, Any]]:
        """
        Burst Scalping:
        - 1 seule position broker (volume unique calculé plus tard)
        - Pas de TP (trailing global)
        - burst_size est virtuel (logique interne)
        """
        import uuid

        if action not in ("BUY", "SELL"):
            return None

        entry_mode = str(sm_cfg.get("entry_mode", "MARKET")).upper()   # MARKET / BUY_LIMIT / SELL_LIMIT
        burst_size = int(sm_cfg.get("burst_size", 8) or 8)
        basket_id  = f"burst_{asset.upper()}_{uuid.uuid4().hex[:8]}"

        return {
            "strategy_type": "scalping",
            "rule_name": "burst_scalping",
            "execution_status": "ready",
            "action": action,
            "asset": asset,
            "order_type": entry_mode,
            "entry_price": float(entry_price) if entry_mode != "MARKET" else None,
            "burst_size": burst_size,                 # virtuel (utile pour ta logique interne)
            "basket_id": basket_id,
            "meta": {
                "burst": True,
                "entry_source": "core_decision"
            }
        }


    def _get_bars(self, asset: str, timeframe: str, count: int):
        """
        Récupère un DataFrame OHLC pour `asset` et `timeframe`.
        Essaie d'abord phase_observer (si dispo), sinon MT5Connector.
        Doit renvoyer un df avec colonnes: ['open','high','low','close','time'] indexé par time.
        """
        try:
            # 1) PhaseObserver (si ton app remonte déjà MTF dans le contexte)
            if hasattr(self, "phase_observer") and hasattr(
                self.phase_observer, "get_bars"
            ):
                df = self.phase_observer.get_bars(asset, timeframe, count)
                if df is not None and len(df) >= min(10, count):
                    return df

            # 2) MT5Connector (fallback standard)
            if hasattr(self, "mt5_connector") and hasattr(
                self.mt5_connector, "get_recent_bars"
            ):
                df = self.mt5_connector.get_recent_bars(asset, timeframe, count)
                return df
        except Exception as e:
            self.logger.warning(f"[{asset}] _get_bars({timeframe}) failed: {e}")

        return None

    def _is_range_environment(self, df15, df5, params) -> bool:
        """
        Confirme un 'vrai' range plat via M15 + M5.
        - M15 définit le couloir (HH/LL sur lookback_m15)
        - M5 doit rester majoritairement à l’intérieur du couloir M15
        et représenter une amplitude 'suffisamment contenue'
        (range5 / range15 < max_consolidation_ratio)
        - Optionnel: vérifier qu'il n'y a pas eu de close > HH15 ou < LL15
        au-delà d'une petite tolérance (anti-breakout).
        """
        if df15 is None or df5 is None:
            return False
        if len(df15) < params["lookback_m15"] or len(df5) < params["lookback_m5"]:
            return False

        recent15 = df15.tail(params["lookback_m15"])
        recent5 = df5.tail(params["lookback_m5"])

        hh15 = float(recent15["high"].max())
        ll15 = float(recent15["low"].min())
        range15 = hh15 - ll15
        if range15 <= 0:
            return False

        hh5 = float(recent5["high"].max())
        ll5 = float(recent5["low"].min())
        range5 = hh5 - ll5

        # M5 doit être "concentré" par rapport à M15
        if (range5 / range15) >= params["max_consolidation_ratio"]:
            return False

        # Anti-breakout: peu (ou pas) de clôtures qui sortent franchement du couloir M15
        tol = params["breakout_tolerance_frac"] * range15
        closes = recent5["close"]
        if (closes > (hh15 + tol)).sum() > params["max_breakout_closes"]:
            return False
        if (closes < (ll15 - tol)).sum() > params["max_breakout_closes"]:
            return False

        return True

    def _rule_liquidity_sweep(
        self,
        df: pd.DataFrame,
        meta: Dict[str, Any],
        lookback: int = 20,
    ) -> Optional[str]:
        """
        Détecte un sweep simple des HH/LL sur 'lookback' barres.
        Contrarian:
        - SELL si le close casse le plus haut récent (sweep au-dessus)
        - BUY  si le close casse le plus bas récent (sweep en-dessous)
        Retourne "BUY" / "SELL" / None.
        """
        try:
            if df is None or lookback is None or int(lookback) < 2:
                return None
            if len(df) < int(lookback):
                return None

            recent = df.iloc[-int(lookback):]

            # Cast robustes
            hi = pd.to_numeric(recent["high"], errors="coerce")
            lo = pd.to_numeric(recent["low"],  errors="coerce")
            cl = pd.to_numeric(df["close"].iloc[-1], errors="coerce")

            hh = float(hi.max()) if np.isfinite(hi.max()) else float("nan")
            ll = float(lo.min()) if np.isfinite(lo.min()) else float("nan")
            close = float(cl) if np.isfinite(cl) else float("nan")

            if not (np.isfinite(hh) and np.isfinite(ll) and np.isfinite(close)):
                return None

            # Heuristique sweep (contrarian)
            if close >= hh:
                return "SELL"
            if close <= ll:
                return "BUY"
            return None
        except Exception:
            return None


    # === [PATTERN MARUBOZU SUPPRIMÉ - Session 23 Nov 2025] ===
    # Fonction _rule_marubozu_range supprimée (215 lignes)
    # Raison : Pattern de bougie jamais utilisé, code mort


    def _rule_range_accumulation(
        self,
        df: pd.DataFrame,
        asset: str,
        price: float,
        action: str,
        meta: Dict[str, Any],
        cfg: Dict[str, Any],
    ) -> Optional[Dict[str, Any]]:
        """
        Détecte un range plat (accumulation) et prend un trade
        sur les extrêmes (haut/bas du range).
        Patché pour intégrer le dernier pattern bougie détecté.
        """
        lookback = int(cfg.get("lookback_bars", 20))
        tolerance = float(cfg.get("tolerance_frac", 0.15))
        if len(df) < lookback:
            return None

        recent = df.tail(lookback)
        hh, ll = float(recent["high"].max()), float(recent["low"].min())
        rng = hh - ll
        if rng <= 0:
            return None

        top_zone = hh - tolerance * rng
        bot_zone = ll + tolerance * rng
        latest_pat = meta.get("latest_pattern")

        dec = None
        if price >= top_zone:
            dec = {
                "action": "SELL",
                "asset": asset,
                "entry_price": price,
                "target_sl_pips": float(cfg.get("sl_pips", 30)),
                "target_tp_pips": float(cfg.get("tp_pips", 30)),
                "rule_name": "range_accumulation_top",
                "strategy_type": "scalping",
                "confidence": 0.7,
            }
        elif price <= bot_zone:
            dec = {
                "action": "BUY",
                "asset": asset,
                "entry_price": price,
                "target_sl_pips": float(cfg.get("sl_pips", 30)),
                "target_tp_pips": float(cfg.get("tp_pips", 30)),
                "rule_name": "range_accumulation_low",
                "strategy_type": "scalping",
                "confidence": 0.7,
            }

        if dec and latest_pat:
            dec["rule_name"] += f"+pattern:{latest_pat.get('pattern')}"
            dec.setdefault("meta", {})["pattern"] = latest_pat

        return dec


    def get_parameters(self) -> Dict[str, any]:
        """
        Retourne un snapshot des paramètres runtime de la stratégie (pour logs/diagnostic).
        """
        try:
            sca_cfg = (self.config_manager.get_strategy_config("scalping") or {}).copy()
        except Exception:
            sca_cfg = {}
        return {
            "name": "scalping",
            "configured": bool(sca_cfg),
            "config_keys": list(sca_cfg.keys()),
        }

    def update_strategy_parameters(self, **kwargs) -> None:
        """
        Mise à jour dynamique de quelques paramètres légers (ex: seuils).
        On reste défensif: on ne casse rien si une clé n’existe pas.
        """
        try:
            sca_cfg = self.config_manager.get_strategy_config("scalping") or {}
            changed = []
            for k, v in kwargs.items():
                if k in sca_cfg:
                    sca_cfg[k] = v
                    changed.append(k)
            if changed:
                # si tu as une API pour renvoyer la config modifiée dans le ConfigManager, appelle-la ici.
                self.logger.info(f"[SCALPING] Params mis à jour: {changed}")
        except Exception as e:
            self.logger.warning(f"[SCALPING] update_strategy_parameters skipped: {e}")

    def evaluate_exit(
        self, context: Dict[str, Any], open_positions: List[Dict[str, Any]]
    ) -> List[Dict[str, Any]]:
        """
        Politique de sortie par défaut (no-op) — renvoie une liste vide si pas de conditions spécifiques.
        L’executor ou d’autres modules peuvent fermer les positions via trailing/SL/TP.
        """
        try:
            # Exemple de garde-fou: on pourrait fermer des paniers "burst" sur condition extrême.
            # Ici, on garde un comportement neutre : pas de force-close.
            return []
        except Exception as e:
            self.logger.warning(f"[SCALPING] evaluate_exit skipped: {e}")
            return []

    # ==========================================================
    # =============      ADAPTATION TP/SL BASE     =============
    # ==========================================================
    def _dynamic_tp_sl_from_vol_atr(
        self, signals: Dict[str, Any], meta: Dict[str, Any], config: Dict[str, Any]
    ) -> Tuple[float, float, str]:
        """
        Calcule SL/TP (en pips) selon la vol% et l’ATR (fallback propre).
        """
        base_sl = float(config.get("stop_loss_pips", 12) or 12)
        base_tp = float(config.get("take_profit_pips", 18) or 18)

        vol_pct = self._extract_vol_pct(signals)
        atr_m5 = float(signals.get("atr_m5", 0.0) or 0.0)
        atr_m5_p = (atr_m5 / meta["pip_size"]) if meta["pip_size"] > 0 else 0.0

        adapt = self.config_manager.get("adaptation_settings", {}) or {}
        vols = adapt.get("volatility_thresholds", {}) or {}
        low, high = float(vols.get("low", 0.05)), float(vols.get("high", 0.5))

        scalping_adapt = adapt.get("scalping", {}) or {}
        sl_high = float(scalping_adapt.get("stop_loss_pips_high_vol", base_sl))
        tp_high = float(scalping_adapt.get("take_profit_pips_high_vol", base_tp))
        sl_low = float(scalping_adapt.get("stop_loss_pips_low_vol", base_sl))
        tp_low = float(scalping_adapt.get("take_profit_pips_low_vol", base_tp))

        if vol_pct >= high:
            sl_pips = max(sl_high, atr_m5_p * 0.8)
            tp_pips = tp_high
            tag = "high_vol"
        elif vol_pct <= low:
            sl_pips = max(sl_low, atr_m5_p * 0.6)
            tp_pips = tp_low
            tag = "low_vol"
        else:
            sl_pips = max(base_sl, atr_m5_p * 0.7)
            tp_pips = base_tp
            tag = "normal_vol"

        # légère correction spread
        spread_pips = meta["spread_pips"]
        tp_pips = max(1.0, tp_pips - spread_pips)
        sl_pips = max(1.0, sl_pips + spread_pips * 0.5)
        return float(sl_pips), float(tp_pips), tag

    # ==========================================================
    # =============            HELPERS            =============
    # ==========================================================

    def _has_blocking_news(self, context: Dict[str, Any]) -> bool:
        try:
            return bool(
                self.config_manager.check_news_schedule(
                    context, context.get("economic_calendar", [])
                )
            )
        except Exception:
            return False

    def _infer_action_from_signals(self, signals: Dict[str, Any]) -> Optional[str]:
        mtf_dir = str(signals.get("mtf_direction", "none")).lower()
        if mtf_dir in {"up", "down"}:
            return "BUY" if mtf_dir == "up" else "SELL"

        phase = str(
            signals.get("phase_memory_stabilized", signals.get("phase", ""))
        ).lower()
        if any(
            k in phase for k in ["bull", "up", "accumulation", "expansion", "trend"]
        ):
            return "BUY"
        if any(k in phase for k in ["bear", "down", "distribution"]):
            return "SELL"
        return None

    def _safe_price_from_signals(self, signals: Dict[str, Any]) -> Optional[float]:
        for k in ("current_price", "last_close", "close", "entry_price"):
            v = signals.get(k)
            try:
                if isinstance(v, (int, float)) and v > 0:
                    return float(v)
            except Exception:
                continue
        return None

    def _safe_asset_meta(
        self,
        asset: str,
        signals: Dict[str, Any],
        context: Dict[str, Any],
        config: Dict[str, Any],
    ) -> Dict[str, Any]:
        md_asset = (context.get("market_data", {}) or {}).get(asset, {}) or {}
        si = (md_asset.get("symbol_info") or config.get("symbol_info") or {}) or {}

        def _num(x, d=0.0):
            try:
                v = float(x)
                return v if math.isfinite(v) else d
            except Exception:
                return d

        point = _num(si.get("point"), 0.00001)
        digits = int(si.get("digits", 5) or 5)
        pip_points = 10.0 if digits in (3, 5) else 1.0
        pip_size = point * pip_points

        spread_points = _num(
            md_asset.get("current_spread_points", signals.get("spread", 0.0)), 0.0
        )
        spread_pips = spread_points / pip_points

        return {
            "point": point,
            "digits": digits,
            "pip_points": pip_points,
            "pip_size": pip_size,
            "spread_pips": spread_pips,
        }

    def _extract_vol_pct(self, signals: Dict[str, Any]) -> float:
        if isinstance(signals.get("volatility_pct"), (int, float)):
            return float(signals["volatility_pct"])
        if isinstance(signals.get("volatility_percentage"), (int, float)):
            return float(signals["volatility_percentage"])
        v = signals.get("volatility")
        if isinstance(v, (int, float)):
            v = float(v)
            return v * 100.0 if v <= 1.0 else v
        return 0.0

    # --- indicateurs génériques (utiles si tu veux enrichir plus tard)
    @staticmethod
    def _sma(series: pd.Series, period: int) -> pd.Series:
        if series is None or period <= 1:
            return series
        return series.rolling(window=period, min_periods=1).mean()

    @staticmethod
    def _std(series: pd.Series, period: int) -> pd.Series:
        if series is None or period <= 1:
            return series * 0
        return series.rolling(window=period, min_periods=1).std(ddof=0)

    @staticmethod
    def _atr(df: pd.DataFrame, period: int = 14) -> float:
        """
        ATR robuste sur Series Pandas (pas de numpy.reduce pour conserver .rolling()).
        Retourne le dernier ATR (float) ou NaN si données insuffisantes.
        """
        try:
            if df is None or len(df) < max(2, int(period)):
                return float("nan")

            h = pd.to_numeric(df["high"], errors="coerce")
            l = pd.to_numeric(df["low"],  errors="coerce")
            c = pd.to_numeric(df["close"], errors="coerce")
            pc = c.shift(1)

            # True Range en Pandas → on garde une Series pour pouvoir .rolling()
            tr = pd.concat([(h - l).abs(), (h - pc).abs(), (l - pc).abs()], axis=1).max(axis=1)

            atr_series = tr.rolling(window=int(period), min_periods=int(period)).mean()
            atr = atr_series.iloc[-1]
            return float(atr) if pd.notna(atr) and atr > 0 else float("nan")
        except Exception:
            return float("nan")
