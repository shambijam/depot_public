# strategy/liquidity.py

from typing import Dict, Any, List, Optional, Tuple
from .base_strategy import BaseStrategy
import math
import pandas as pd
import numpy as np
from phase_observer.market_analyzer import MarketAnalyzer
from phase_observer.detectors import Detectors



class LiquidityStrategy(BaseStrategy):
    """
    Stratégie 'Liquidity' — institutionnelle
    - Switch automatique quand une zone de liquidité est identifiée
    - Entrée selon entry_logic (break_of_absorption_extreme ou retracement OB/FVG)
    - SL derrière l'extrême du sweep (fallback ATR)
    - TP vers la prochaine poche de liquidité (EQH/EQL > OB > FVG ; fallback ATR)
    - Respecte decision_engine.execution (order_type = limit, mitigation, etc.)
    """

    # =========================
    #         LIFECYCLE
    # =========================
    def __init__(
        self,
        config_manager,
        strategy_config: Optional[Dict[str, Any]] = None,
        logger=None,
    ):
        """
        Initialise la stratégie Liquidity.
        """
        super().__init__(config_manager, strategy_config or {})

        self.config_manager = config_manager
        self.strategy_config = strategy_config or {}
        self.logger = logger or getattr(config_manager, "logger", None)

        # Initialisation des détecteurs de liquidité
        try:
            self.detectors = Detectors(logger=self.logger, config_manager=config_manager)
            self.logger.info("✅ Détecteurs de liquidité initialisés avec succès.")
        except Exception as e:
            self.logger.error(f"❌ Erreur initialisation Detectors: {e}", exc_info=True)
            self.detectors = None

        self.logger.info("Moteur de stratégie Liquidity initialisé.")

       # =========================
    #      PUBLIC METHODS
    # =========================
    def evaluate_entry(
        self,
        analyzed_context: Dict[str, Any],
        asset_signals: Dict[str, Dict[str, Any]],
    ) -> Dict[str, Any]:
        """
        Dispatcher multi-actifs :
        - analyzed_context : contexte global
        - asset_signals : {"EURUSD": {...}, "GBPUSD": {...}, ...}
        Retourne un dict {asset: decision} (seuls les actifs avec setup valide sont présents).
        """
        results: Dict[str, Any] = {}
        if not isinstance(asset_signals, dict):
            return results

        for asset, sig in asset_signals.items():
            try:
                dec = self._evaluate_single_asset(asset, analyzed_context, sig or {})
                if dec:
                    results[asset] = dec
            except Exception as e:
                self.logger.error(f"[LIQUIDITY] evaluate_entry error on {asset}: {e}", exc_info=True)

        return results

    # =========================
    #   LOGIQUE PAR ACTIF
    # =========================

    def _detect_liquidity_signals(
        self,
        asset: str,
        df: pd.DataFrame,
        df_htf: Optional[pd.DataFrame] = None
    ) -> Dict[str, Any]:
        """
        Appelle les 8 détecteurs de liquidité et retourne un dict enrichi.

        Retourne:
            {
                "sweep_details": {...},
                "ob_details": {...},
                "fvg_details": {...},
                "eqh_eql_details": {...},
                "bos_mss_details": {...},
                "absorption_details": {...},
                "market_regime": "...",
                "micro_phase": "..."
            }
        """
        if self.detectors is None:
            self.logger.warning(f"[{asset}] Détecteurs non disponibles, retour vide.")
            return {}

        signals = {}

        try:
            # 1. Liquidity Sweeps (prioritaire pour SL/Entry)
            sweeps = self.detectors.detect_liquidity_sweeps(df, df_htf)
            if sweeps and len(sweeps) > 0:
                latest_sweep = sweeps[-1]
                if latest_sweep is not None:
                    signals["sweep_details"] = latest_sweep
                    self.logger.debug(f"[{asset}] Sweep détecté: {latest_sweep.get('type')} @ {latest_sweep.get('price')}")
        except Exception as e:
            self.logger.debug(f"[{asset}] detect_liquidity_sweeps error: {e}")

        try:
            # 2. Order Blocks (zones d'entrée + TP)
            obs = self.detectors.detect_order_block_ml_enhanced(df, df_htf)
            if obs and len(obs) > 0:
                latest_ob = obs[-1]
                if latest_ob is not None:
                    signals["ob_details"] = latest_ob
                    self.logger.debug(f"[{asset}] OB détecté: {latest_ob.get('type')} @ {latest_ob.get('zone')}")
        except Exception as e:
            self.logger.debug(f"[{asset}] detect_order_block_ml_enhanced error: {e}")

        try:
            # 3. Fair Value Gaps (zones d'entrée + TP)
            fvgs = self.detectors.detect_fvg_enhanced(df)
            if fvgs and len(fvgs) > 0:
                latest_fvg = fvgs[-1]
                if latest_fvg is not None:
                    signals["fvg_details"] = latest_fvg
                    self.logger.debug(f"[{asset}] FVG détecté: {latest_fvg.get('type')} @ {latest_fvg.get('zone')}")
        except Exception as e:
            self.logger.debug(f"[{asset}] detect_fvg_enhanced error: {e}")

        try:
            # 4. Equal Highs/Lows (TP prioritaire)
            eqh_eql = self.detectors.detect_eqh_eql(df)
            if eqh_eql and len(eqh_eql) > 0:
                latest_eq = eqh_eql[-1]
                if latest_eq is not None:
                    signals["eqh_eql_details"] = latest_eq
                    self.logger.debug(f"[{asset}] EQH/EQL détecté: {latest_eq.get('type')} @ {latest_eq.get('level')}")
        except Exception as e:
            self.logger.debug(f"[{asset}] detect_eqh_eql error: {e}")

        try:
            # 5. Break of Structure / Market Structure Shift (SL)
            bos_mss = self.detectors.detect_bos_mss_enhanced(df, df_htf)
            if bos_mss and len(bos_mss) > 0:
                latest_bos = bos_mss[-1]
                if latest_bos is not None:
                    signals["bos_mss_details"] = latest_bos
                    self.logger.debug(f"[{asset}] BOS/MSS détecté: {latest_bos.get('type')} @ {latest_bos.get('level')}")
        except Exception as e:
            self.logger.debug(f"[{asset}] detect_bos_mss_enhanced error: {e}")

        try:
            # 6. Absorption (extrêmes)
            absorption = self.detectors.detect_absorption(df)
            if absorption and len(absorption) > 0:
                latest_abs = absorption[-1]
                if latest_abs is not None:
                    signals["absorption_details"] = latest_abs
                    self.logger.debug(f"[{asset}] Absorption détectée: {latest_abs.get('type')}")
        except Exception as e:
            self.logger.debug(f"[{asset}] detect_absorption error: {e}")

        try:
            # 7. Market Regime (contexte global)
            regime = self.detectors.detect_market_regime(df)
            if regime is not None and not regime.empty:
                signals["market_regime"] = str(regime.iloc[-1])
                self.logger.debug(f"[{asset}] Regime: {signals['market_regime']}")
        except Exception as e:
            self.logger.debug(f"[{asset}] detect_market_regime error: {e}")

        try:
            # 8. Micro Phase M1 (phase courte durée)
            micro = self.detectors.detect_micro_phase_m1(df)
            if micro and len(micro) > 0:
                latest_micro = micro[-1]
                if latest_micro is not None:
                    signals["micro_phase"] = str(latest_micro.get("phase", "unknown"))
                    self.logger.debug(f"[{asset}] Micro phase: {signals['micro_phase']}")
        except Exception as e:
            self.logger.debug(f"[{asset}] detect_micro_phase_m1 error: {e}")

        # Log résumé
        detected_count = sum(1 for k in signals.keys() if k.endswith("_details"))
        self.logger.info(f"[{asset}] 🔍 Détection liquidité: {detected_count}/6 signaux détectés")

        return signals

        # --- helpers nécessaires par _evaluate_single_asset -----------------------
    def _safe_asset_meta(
        self,
        asset: str,
        signals: Dict[str, Any],
        context: Dict[str, Any],
        strat_cfg: Dict[str, Any],
    ) -> Dict[str, Any]:
        """
        Assemble proprement les métadonnées utilisées par evaluate_single_asset.
        Remplit pip_size et spread_pips (indispensables pour les gardes-fous).
        """
        md_all = (context.get("market_data") or {})
        ctx_md = md_all.get(asset, {}) or {}
        # Certaines implémentations stockent symbol_info dans le contexte global
        symbol_info = ctx_md.get("symbol_info") or context.get("symbol_info") or {}

        # Basics broker
        point = float(symbol_info.get("point", 0.0001) or 0.0001)
        digits = int(symbol_info.get("digits", 5) or 5)

        # pip_size robuste : par défaut 10 * point (FX 5 digits → 0.0001 ; XAU point=0.01 → 0.1)
        pip_size = float(symbol_info.get("pip_size", point * 10.0))

        # spread en "points" côté broker; converti en pips pour les checks
        # Ex: points_per_pip = pip_size / point (FX 5 digits: 0.0001 / 0.00001 = 10)
        raw_spread_points = float(symbol_info.get("spread", signals.get("current_spread_points", 0.0)) or 0.0)
        points_per_pip = (pip_size / point) if point > 0 else 10.0
        spread_pips = float(raw_spread_points) / float(points_per_pip) if points_per_pip > 0 else raw_spread_points

        return {
            "asset": asset,
            "phase": signals.get("phase") or "unknown",
            "confidence": float(signals.get("confidence", 0.0) or 0.0),
            "volatility": float(signals.get("volatility", 0.0) or 0.0),
            "rule": signals.get("rule") or strat_cfg.get("rule", "default"),
            # clés attendues par le code appelant :
            "pip_size": float(pip_size),
            "spread_pips": float(spread_pips),
            "point": float(point),
            "digits": int(digits),
        }

    def _safe_price_from_signals(self, signals: Dict[str, Any]) -> Optional[float]:
        """
        Récupère un prix exploitable depuis les signaux (sans lever d'exception).
        Ordre de priorité: entry_price > price > last > mid > (ask+bid)/2 > close.
        """
        for k in ("entry_price", "price", "last", "mid", "close", "ask", "bid"):
            v = signals.get(k)
            try:
                if v is not None:
                    v = float(v)
                    if k in ("ask", "bid") and k in ("ask", "bid"):
                        # si un seul (ask ou bid) est dispo on le prend; si les deux existent mid sera mieux
                        pass
                    return v
            except Exception:
                continue

        # mid si ask/bid dispo
        ask = signals.get("ask")
        bid = signals.get("bid")
        try:
            if ask is not None and bid is not None:
                return (float(ask) + float(bid)) / 2.0
        except Exception:
            pass

        return None
    
    def _infer_action_from_signals(self, signals: Dict[str, Any]) -> Optional[str]:
        """
        Déduit BUY / SELL à partir des signaux.
        - Cherche d'abord 'action' explicite
        - Sinon mappe 'bias' ou 'regime' (bull → BUY, bear → SELL)
        - Fallback None si pas clair
        """
        if not isinstance(signals, dict):
            return None

        # 1) Action explicite
        action = signals.get("action") or signals.get("direction") or signals.get("side")
        if isinstance(action, str):
            a = action.strip().upper()
            if a in ("BUY", "SELL"):
                return a

        # 2) Bias
        bias = signals.get("bias")
        if isinstance(bias, str):
            b = bias.strip().upper()
            if "BULL" in b or "LONG" in b:
                return "BUY"
            if "BEAR" in b or "SHORT" in b:
                return "SELL"

        # 3) Régime du signal
        regime = signals.get("regime") or signals.get("market_regime")
        if isinstance(regime, str):
            r = regime.lower()
            if "bull" in r:
                return "BUY"
            if "bear" in r:
                return "SELL"

        # 4) Rien trouvé
        return None

    def _log_liquidity_consolidated_report(
        self,
        asset: str,
        liquidity_signals: Dict[str, Any],
        decision: Optional[Dict[str, Any]],
        price: float,
        pip_size: float
    ) -> None:
        """
        🔷 Bilan consolidé liquidity - Format similaire à OrderFlow V6

        Affiche :
        - [1] DÉTECTEURS INSTITUTIONNELS (8/8)
        - [2] ANALYSE CONFLUENCE
        - [3] DÉCISION FINALE
        """
        from datetime import datetime

        timestamp = datetime.now().strftime("%d %b %Y %H:%M:%S")

        # Header
        self.logger.info("═" * 71)
        self.logger.info(f"🔷 BILAN LIQUIDITÉ | {asset} | {timestamp}")
        self.logger.info("═" * 71)

        # ========================================================================
        # [1] DÉTECTEURS INSTITUTIONNELS (8/8)
        # ========================================================================
        self.logger.info("")
        self.logger.info("[1] DÉTECTEURS INSTITUTIONNELS (8/8)")
        self.logger.info("─" * 71)

        # 1. Sweep
        sweep = liquidity_signals.get("sweep_details")
        if sweep:
            side = sweep.get("side", "N/A")
            sweep_price = sweep.get("price", 0.0)
            wick_ratio = sweep.get("wick_ratio", 0.0)
            dist_pips = sweep.get("dist_pips", 0.0)
            vol_z = sweep.get("volume_z", 0.0)
            self.logger.info(
                f"  ✅ Sweep        : {side.upper()} @ {sweep_price:.5f} | "
                f"wick={wick_ratio:.1f}x | dist={dist_pips:.1f}p | vol_z={vol_z:.1f}"
            )
        else:
            self.logger.info("  ❌ Sweep        : Aucun détecté")

        # 2. EQH/EQL
        eqh_eql = liquidity_signals.get("eqh_eql_details")
        if eqh_eql:
            eq_type = eqh_eql.get("type", "N/A").upper()
            eq_price = eqh_eql.get("price", eqh_eql.get("level", 0.0))
            touches = eqh_eql.get("touches", 0)
            quality = eqh_eql.get("quality", "UNKNOWN").upper()
            self.logger.info(
                f"  ✅ EQH/EQL      : {eq_type} @ {eq_price:.5f} | "
                f"touches={touches} | qualité={quality}"
            )
        else:
            self.logger.info("  ❌ EQH/EQL      : Aucun détecté")

        # 3. Order Block
        ob = liquidity_signals.get("ob_details")
        if ob:
            ob_type = ob.get("type", "N/A")
            ob_zone = ob.get("zone", [0.0, 0.0])
            ob_zone_str = f"{ob_zone[0]:.5f}-{ob_zone[1]:.5f}" if isinstance(ob_zone, list) and len(ob_zone) == 2 else "N/A"
            self.logger.info(f"  ✅ Order Block  : {ob_type} @ {ob_zone_str}")
        else:
            self.logger.info("  ❌ Order Block  : Aucun détecté")

        # 4. FVG
        fvg = liquidity_signals.get("fvg_details")
        if fvg:
            fvg_type = fvg.get("type", "N/A")
            fvg_zone = fvg.get("zone", [0.0, 0.0])
            fvg_zone_str = f"{fvg_zone[0]:.5f}-{fvg_zone[1]:.5f}" if isinstance(fvg_zone, list) and len(fvg_zone) == 2 else "N/A"
            self.logger.info(f"  ✅ FVG          : {fvg_type} @ {fvg_zone_str}")
        else:
            self.logger.info("  ❌ FVG          : Aucun détecté")

        # 5. BOS/MSS
        bos = liquidity_signals.get("bos_mss_details")
        if bos:
            bos_type = bos.get("type", "N/A")
            bos_level = bos.get("level", 0.0)
            self.logger.info(f"  ✅ BOS/MSS      : {bos_type} @ {bos_level:.5f}")
        else:
            self.logger.info("  ❌ BOS/MSS      : Aucun détecté")

        # 6. Absorption
        absorption = liquidity_signals.get("absorption_details")
        if absorption:
            abs_type = absorption.get("type", "N/A")
            self.logger.info(f"  ✅ Absorption   : {abs_type}")
        else:
            self.logger.info("  ❌ Absorption   : Aucune")

        # 7. Market Regime
        regime = liquidity_signals.get("market_regime")
        if regime:
            self.logger.info(f"  ✅ Regime       : {regime}")
        else:
            self.logger.info("  ❌ Regime       : Inconnu")

        # 8. Micro Phase
        micro = liquidity_signals.get("micro_phase")
        if micro:
            self.logger.info(f"  ✅ Micro Phase  : {micro}")
        else:
            self.logger.info("  ❌ Micro Phase  : Inconnue")

        # ========================================================================
        # [2] ANALYSE CONFLUENCE
        # ========================================================================
        self.logger.info("")
        self.logger.info("[2] ANALYSE CONFLUENCE")
        self.logger.info("─" * 71)

        if decision:
            # Setup détecté
            rule_name = decision.get("rule_name", "unknown")
            setup_label = ""

            # Phase 1 setups
            if rule_name == "liquidity_sweep_eql":
                setup_label = "⚡ SWEEP + EQL (BUY)"
            elif rule_name == "liquidity_sweep_eqh":
                setup_label = "⚡ SWEEP + EQH (SELL)"

            # Phase 2 setups
            elif rule_name == "liquidity_ob_fvg_buy":
                setup_label = "⚡ ORDER BLOCK + FVG (BUY)"
            elif rule_name == "liquidity_ob_fvg_sell":
                setup_label = "⚡ ORDER BLOCK + FVG (SELL)"
            elif rule_name == "liquidity_bos_absorption_buy":
                setup_label = "⚡ BOS + ABSORPTION (BUY)"
            elif rule_name == "liquidity_bos_absorption_sell":
                setup_label = "⚡ BOS + ABSORPTION (SELL)"
            elif rule_name == "liquidity_micro_phase_buy":
                setup_label = "⚡ MICRO PHASE REVERSAL (BUY)"
            elif rule_name == "liquidity_micro_phase_sell":
                setup_label = "⚡ MICRO PHASE REVERSAL (SELL)"

            else:
                setup_label = f"⚡ {rule_name.upper()}"

            self.logger.info(f"  Setup détecté   : {setup_label}")

            # Distance
            if sweep and eqh_eql:
                sweep_price = float(sweep.get("price", price))
                eq_price = float(eqh_eql.get("price", eqh_eql.get("level", price)))
                distance_pips = abs(sweep_price - eq_price) / pip_size
                distance_status = "✅" if distance_pips < 50 else "❌"
                self.logger.info(f"  Distance        : {distance_status} {distance_pips:.1f} pips (< 50p)")
            else:
                self.logger.info(f"  Distance        : N/A")

            # Entry / SL / TP
            entry = decision.get("entry_price", 0.0)
            sl = decision.get("sl", 0.0)
            tp = decision.get("tp", 0.0)

            sl_pips = abs(entry - sl) / pip_size if pip_size > 0 else 0
            tp_pips = abs(tp - entry) / pip_size if pip_size > 0 else 0

            self.logger.info(f"  Entry           : {entry:.5f} (market)")
            self.logger.info(f"  Stop Loss       : {sl:.5f} ({sl_pips:.1f}p)")
            self.logger.info(f"  Take Profit     : {tp:.5f} ({tp_pips:.1f}p)")

            # RR
            rr = decision.get("rr", 0.0)
            self.logger.info(f"  Risk/Reward     : {rr:.2f}")

        else:
            # Aucun setup
            self.logger.info("  Setup détecté   : ❌ Aucun")

            # Raisons possibles
            if not sweep:
                self.logger.info("  Raison          : Aucun sweep de liquidité")
            elif not eqh_eql:
                self.logger.info("  Raison          : Aucun EQH/EQL détecté")
            elif sweep and eqh_eql:
                sweep_price = float(sweep.get("price", price))
                eq_price = float(eqh_eql.get("price", eqh_eql.get("level", price)))
                distance_pips = abs(sweep_price - eq_price) / pip_size
                if distance_pips >= 50:
                    self.logger.info(f"  Raison          : Distance trop grande ({distance_pips:.1f}p > 50p)")
                else:
                    sweep_side = sweep.get("side", "").lower()
                    eq_type = eqh_eql.get("type", "").lower()
                    if not ((sweep_side == "buy" and eq_type == "eql") or (sweep_side == "sell" and eq_type == "eqh")):
                        self.logger.info(f"  Raison          : Confluence invalide ({sweep_side} + {eq_type})")
                    else:
                        self.logger.info("  Raison          : Confluence non validée")
            else:
                self.logger.info("  Raison          : Signaux insuffisants")

        # ========================================================================
        # [3] DÉCISION FINALE
        # ========================================================================
        self.logger.info("")
        self.logger.info("[3] DÉCISION FINALE")
        self.logger.info("─" * 71)

        if decision:
            action = decision.get("action", "N/A")
            confidence = decision.get("confidence", 0.0) * 100
            rule = decision.get("rule_name", "N/A")
            status = decision.get("execution_status", "N/A").upper()

            self.logger.info(f"  Action          : ✅ {action}")
            self.logger.info(f"  Confidence      : {confidence:.0f}%")
            self.logger.info(f"  Rule            : {rule}")
            self.logger.info(f"  Status          : {status}")
        else:
            self.logger.info("  Action          : ❌ AUCUNE")
            self.logger.info("  Confidence      : 0%")
            self.logger.info("  Rule            : N/A")
            self.logger.info("  Status          : WAITING")

        # Footer
        self.logger.info("")
        self.logger.info("═" * 71)


    def _evaluate_single_asset(
        self,
        asset: str,
        analyzed_context: Dict[str, Any],
        asset_signals: Dict[str, Any],
    ) -> Dict[str, Any]:
        """
        **Reprise 1:1 de ta logique d’origine**, mais en scope mono-actif.
        Rien d’autre n’est modifié.
        """
        try:
            # --- 0) Données & config ---
            ctx_md = (analyzed_context.get("market_data") or {}).get(asset, {}) or {}

            # Sélection sécurisée du DataFrame (évite les erreurs pandas en booléen)
            df_m1 = None
            for key in ("df_m1", "rates_df", "annotated_rates_df", "df"):
                val = ctx_md.get(key)
                if isinstance(val, pd.DataFrame) and not val.empty:
                    df_m1 = val
                    break

            df_work = df_m1.copy() if isinstance(df_m1, pd.DataFrame) and len(df_m1) >= 50 else None

            # --- 0.5) Détection des signaux de liquidité (Session 23 Nov 2025) ---
            # Récupération des DataFrames HTF si disponibles
            df_htf = None
            for key in ("df_htf", "rates_df_h1", "df_h1"):
                val = ctx_md.get(key)
                if isinstance(val, pd.DataFrame) and not val.empty:
                    df_htf = val
                    break

            # Appel des 8 détecteurs de liquidité
            if df_work is not None:
                liquidity_signals = self._detect_liquidity_signals(asset, df_work, df_htf)
                # Fusion avec asset_signals pour que les fonctions aval les trouvent
                asset_signals = {**asset_signals, **liquidity_signals}
            else:
                self.logger.warning(f"[{asset}] df_work non disponible, détection liquidité skip.")
                liquidity_signals = {}  # Initialisation pour éviter NameError

            # === [DÉTECTION PATTERNS SUPPRIMÉE - Session 23 Nov 2025] ===
            # Bloc MarketAnalyzer patterns retiré (16 lignes)
            # Raison : Détecteurs de patterns/bougies supprimés du système
            # latest_pattern causait ValueError (pandas.Series ambiguity)

            strat_cfg = (self.strategy_config or {}).copy()

            # Config burst_scalping
            burst_cfg = (
                strat_cfg.get("burst_scalping")
                or ((strat_cfg.get("entry_rules") or {}).get("scalping") or {}).get(
                    "burst_scalping"
                )
                or {}
            )

            # --- 1) Métadonnées ---
            meta = self._safe_asset_meta(asset, asset_signals, analyzed_context, strat_cfg)
            pip_size = meta.get("pip_size", 0.0)
            if pip_size <= 0:
                self.logger.warning(f"[{asset}] pip_size invalide.")
                return {}

            # --- 2) Prix courant ---
            price = self._safe_price_from_signals(asset_signals)
            if not price:
                self.logger.info(f"[{asset}] Pas de prix exploitable dans les signaux.")
                return {}

            # === [APPELS MARUBOZU FANTÔMES SUPPRIMÉS - Session 23 Nov 2025] ===
            # Les appels à _rule_marubozu_playbook et _rule_marubozu_impulse ont été supprimés
            # Raison : Ces fonctions n'existent PAS dans liquidity.py (code mort causant AttributeError)

            # --- 4) Biais directionnel ---
            action = self._infer_action_from_signals(asset_signals)
            if action is None:
                self.logger.info(f"[{asset}] Aucune direction claire.")
                return {}


            # ========================================================================
            # 🎯 LOGIQUE LIQUIDITÉ - Analyse des setups institutionnels
            # ========================================================================
            # ✅ Phase 1 : Implémentation minimale (Sweep + EQH/EQL)
            # Les 8 détecteurs ont retourné leurs signaux dans liquidity_signals

            sweep = liquidity_signals.get("sweep_details")
            eqh_eql = liquidity_signals.get("eqh_eql_details")

            # --- Setup 1 : Liquidity Sweep + Equal Low (BUY) ---
            if sweep and eqh_eql:
                self.logger.info(
                    f"[LIQUIDITY][{asset}] 🔍 Analyse confluence | "
                    f"sweep={'✅' if sweep else '❌'} | eqh_eql={'✅' if eqh_eql else '❌'}"
                )

                # BUY Setup : Sweep sell-side + Equal Low
                if sweep.get("side") == "buy" and eqh_eql.get("type") == "eql":
                    sweep_price = float(sweep.get("price", price))
                    eql_price = float(eqh_eql.get("price", price))

                    # Validation : EQL doit être proche (< 50 pips)
                    distance_pips = abs(sweep_price - eql_price) / pip_size
                    if distance_pips < 50:
                        entry = price  # Entrée au marché
                        sl = sweep_price - (15 * pip_size)  # Stop 15 pips sous le sweep
                        tp = eql_price  # Target = Equal Low

                        # Calcul RR
                        sl_pips = abs(entry - sl) / pip_size
                        tp_pips = abs(tp - entry) / pip_size
                        rr = tp_pips / sl_pips if sl_pips > 0 else 0

                        self.logger.info(
                            f"[LIQUIDITY][{asset}] ⚡ SETUP VALIDE | sweep_eql | "
                            f"Entry={entry:.5f} | SL={sl:.5f} ({sl_pips:.1f}p) | "
                            f"TP={tp:.5f} ({tp_pips:.1f}p) | RR={rr:.2f}"
                        )

                        decision = {
                            "asset": asset,
                            "action": "BUY",
                            "entry_price": entry,
                            "sl": sl,
                            "tp": tp,
                            "confidence": 0.70,
                            "rule_name": "liquidity_sweep_eql",
                            "strategy_type": "liquidity",
                            "execution_status": "ready",
                            "signals": liquidity_signals,
                            "meta": meta,
                            "rr": rr
                        }

                        # 🔷 Afficher le bilan consolidé liquidity
                        self._log_liquidity_consolidated_report(asset, liquidity_signals, decision, price, pip_size)

                        return decision
                    else:
                        self.logger.info(
                            f"[LIQUIDITY][{asset}] ⚠️ Distance trop grande | "
                            f"sweep-eql={distance_pips:.1f}p (max 50p)"
                        )

                # SELL Setup : Sweep buy-side + Equal High
                elif sweep.get("side") == "sell" and eqh_eql.get("type") == "eqh":
                    sweep_price = float(sweep.get("price", price))
                    eqh_price = float(eqh_eql.get("price", price))

                    # Validation : EQH doit être proche (< 50 pips)
                    distance_pips = abs(sweep_price - eqh_price) / pip_size
                    if distance_pips < 50:
                        entry = price  # Entrée au marché
                        sl = sweep_price + (15 * pip_size)  # Stop 15 pips au-dessus du sweep
                        tp = eqh_price  # Target = Equal High

                        # Calcul RR
                        sl_pips = abs(sl - entry) / pip_size
                        tp_pips = abs(entry - tp) / pip_size
                        rr = tp_pips / sl_pips if sl_pips > 0 else 0

                        self.logger.info(
                            f"[LIQUIDITY][{asset}] ⚡ SETUP VALIDE | sweep_eqh | "
                            f"Entry={entry:.5f} | SL={sl:.5f} ({sl_pips:.1f}p) | "
                            f"TP={tp:.5f} ({tp_pips:.1f}p) | RR={rr:.2f}"
                        )

                        decision = {
                            "asset": asset,
                            "action": "SELL",
                            "entry_price": entry,
                            "sl": sl,
                            "tp": tp,
                            "confidence": 0.70,
                            "rule_name": "liquidity_sweep_eqh",
                            "strategy_type": "liquidity",
                            "execution_status": "ready",
                            "signals": liquidity_signals,
                            "meta": meta,
                            "rr": rr
                        }

                        # 🔷 Afficher le bilan consolidé liquidity
                        self._log_liquidity_consolidated_report(asset, liquidity_signals, decision, price, pip_size)

                        return decision
                    else:
                        self.logger.info(
                            f"[LIQUIDITY][{asset}] ⚠️ Distance trop grande | "
                            f"sweep-eqh={distance_pips:.1f}p (max 50p)"
                        )

            # ========================================================================
            # 🎯 PHASE 2 : Setups additionnels exploitant les 6 autres détecteurs
            # ========================================================================

            ob = liquidity_signals.get("ob_details")
            fvg = liquidity_signals.get("fvg_details")
            bos_mss = liquidity_signals.get("bos_mss_details")
            absorption = liquidity_signals.get("absorption_details")
            regime = liquidity_signals.get("market_regime", "").lower()
            micro_phase = liquidity_signals.get("micro_phase", "").lower()

            # --- Setup 3 : Order Block + Fair Value Gap (BUY) ---
            if ob and fvg:
                ob_type = ob.get("type", "").lower()
                fvg_type = fvg.get("type", "").lower()

                # BUY Setup : OB bullish + FVG bullish
                if "bull" in ob_type and "bull" in fvg_type:
                    ob_zone = ob.get("zone", [0.0, 0.0])
                    fvg_zone = fvg.get("zone", [0.0, 0.0])

                    if isinstance(ob_zone, list) and len(ob_zone) == 2 and isinstance(fvg_zone, list) and len(fvg_zone) == 2:
                        ob_min, ob_max = float(ob_zone[0]), float(ob_zone[1])
                        fvg_min, fvg_max = float(fvg_zone[0]), float(fvg_zone[1])

                        # Validation : FVG au-dessus de l'OB (confluence)
                        if fvg_min >= ob_max:
                            distance_pips = (fvg_min - ob_max) / pip_size
                            if distance_pips < 30:  # FVG proche de l'OB
                                entry = price
                                sl = ob_min - (10 * pip_size)  # SL sous l'OB
                                tp = fvg_max + (distance_pips * pip_size)  # TP au-dessus FVG

                                sl_pips = abs(entry - sl) / pip_size
                                tp_pips = abs(tp - entry) / pip_size
                                rr = tp_pips / sl_pips if sl_pips > 0 else 0

                                if rr >= 1.5:  # Filtre RR minimum
                                    self.logger.info(
                                        f"[LIQUIDITY][{asset}] ⚡ SETUP VALIDE | ob_fvg_buy | "
                                        f"Entry={entry:.5f} | SL={sl:.5f} ({sl_pips:.1f}p) | "
                                        f"TP={tp:.5f} ({tp_pips:.1f}p) | RR={rr:.2f}"
                                    )

                                    decision = {
                                        "asset": asset,
                                        "action": "BUY",
                                        "entry_price": entry,
                                        "sl": sl,
                                        "tp": tp,
                                        "confidence": 0.65,
                                        "rule_name": "liquidity_ob_fvg_buy",
                                        "strategy_type": "liquidity",
                                        "execution_status": "ready",
                                        "signals": liquidity_signals,
                                        "meta": meta,
                                        "rr": rr
                                    }

                                    self._log_liquidity_consolidated_report(asset, liquidity_signals, decision, price, pip_size)
                                    return decision

                # SELL Setup : OB bearish + FVG bearish
                elif "bear" in ob_type and "bear" in fvg_type:
                    ob_zone = ob.get("zone", [0.0, 0.0])
                    fvg_zone = fvg.get("zone", [0.0, 0.0])

                    if isinstance(ob_zone, list) and len(ob_zone) == 2 and isinstance(fvg_zone, list) and len(fvg_zone) == 2:
                        ob_min, ob_max = float(ob_zone[0]), float(ob_zone[1])
                        fvg_min, fvg_max = float(fvg_zone[0]), float(fvg_zone[1])

                        # Validation : FVG en-dessous de l'OB
                        if fvg_max <= ob_min:
                            distance_pips = (ob_min - fvg_max) / pip_size
                            if distance_pips < 30:
                                entry = price
                                sl = ob_max + (10 * pip_size)  # SL au-dessus de l'OB
                                tp = fvg_min - (distance_pips * pip_size)  # TP en-dessous FVG

                                sl_pips = abs(sl - entry) / pip_size
                                tp_pips = abs(entry - tp) / pip_size
                                rr = tp_pips / sl_pips if sl_pips > 0 else 0

                                if rr >= 1.5:
                                    self.logger.info(
                                        f"[LIQUIDITY][{asset}] ⚡ SETUP VALIDE | ob_fvg_sell | "
                                        f"Entry={entry:.5f} | SL={sl:.5f} ({sl_pips:.1f}p) | "
                                        f"TP={tp:.5f} ({tp_pips:.1f}p) | RR={rr:.2f}"
                                    )

                                    decision = {
                                        "asset": asset,
                                        "action": "SELL",
                                        "entry_price": entry,
                                        "sl": sl,
                                        "tp": tp,
                                        "confidence": 0.65,
                                        "rule_name": "liquidity_ob_fvg_sell",
                                        "strategy_type": "liquidity",
                                        "execution_status": "ready",
                                        "signals": liquidity_signals,
                                        "meta": meta,
                                        "rr": rr
                                    }

                                    self._log_liquidity_consolidated_report(asset, liquidity_signals, decision, price, pip_size)
                                    return decision

            # --- Setup 4 : Break of Structure + Absorption (BUY) ---
            if bos_mss and absorption:
                bos_type = bos_mss.get("type", "").lower()
                abs_side = absorption.get("side", "").lower()

                # BUY Setup : BOS bullish + Absorption buy-side
                if "bull" in bos_type and abs_side == "buy":
                    bos_level = float(bos_mss.get("level", price))
                    body_ratio = float(absorption.get("body_ratio", 0.0))

                    # Validation : Absorption forte (body_ratio >= 0.6)
                    if body_ratio >= 0.6:
                        entry = price
                        sl = bos_level - (20 * pip_size)  # SL sous le BOS
                        sl_pips = abs(entry - sl) / pip_size
                        tp = entry + (sl_pips * 2.0 * pip_size)  # TP = 2x SL (RR 2.0)

                        tp_pips = abs(tp - entry) / pip_size
                        rr = tp_pips / sl_pips if sl_pips > 0 else 0

                        self.logger.info(
                            f"[LIQUIDITY][{asset}] ⚡ SETUP VALIDE | bos_absorption_buy | "
                            f"Entry={entry:.5f} | SL={sl:.5f} ({sl_pips:.1f}p) | "
                            f"TP={tp:.5f} ({tp_pips:.1f}p) | RR={rr:.2f}"
                        )

                        decision = {
                            "asset": asset,
                            "action": "BUY",
                            "entry_price": entry,
                            "sl": sl,
                            "tp": tp,
                            "confidence": 0.68,
                            "rule_name": "liquidity_bos_absorption_buy",
                            "strategy_type": "liquidity",
                            "execution_status": "ready",
                            "signals": liquidity_signals,
                            "meta": meta,
                            "rr": rr
                        }

                        self._log_liquidity_consolidated_report(asset, liquidity_signals, decision, price, pip_size)
                        return decision

                # SELL Setup : BOS bearish + Absorption sell-side
                elif "bear" in bos_type and abs_side == "sell":
                    bos_level = float(bos_mss.get("level", price))
                    body_ratio = float(absorption.get("body_ratio", 0.0))

                    if body_ratio >= 0.6:
                        entry = price
                        sl = bos_level + (20 * pip_size)  # SL au-dessus du BOS
                        sl_pips = abs(sl - entry) / pip_size
                        tp = entry - (sl_pips * 2.0 * pip_size)  # TP = 2x SL (RR 2.0)

                        tp_pips = abs(entry - tp) / pip_size
                        rr = tp_pips / sl_pips if sl_pips > 0 else 0

                        self.logger.info(
                            f"[LIQUIDITY][{asset}] ⚡ SETUP VALIDE | bos_absorption_sell | "
                            f"Entry={entry:.5f} | SL={sl:.5f} ({sl_pips:.1f}p) | "
                            f"TP={tp:.5f} ({tp_pips:.1f}p) | RR={rr:.2f}"
                        )

                        decision = {
                            "asset": asset,
                            "action": "SELL",
                            "entry_price": entry,
                            "sl": sl,
                            "tp": tp,
                            "confidence": 0.68,
                            "rule_name": "liquidity_bos_absorption_sell",
                            "strategy_type": "liquidity",
                            "execution_status": "ready",
                            "signals": liquidity_signals,
                            "meta": meta,
                            "rr": rr
                        }

                        self._log_liquidity_consolidated_report(asset, liquidity_signals, decision, price, pip_size)
                        return decision

            # --- Setup 5 : Micro Phase Reversal (BUY) ---
            if micro_phase and regime:
                # BUY Setup : Micro phase = accumulation + Regime favorable
                if micro_phase == "accumulation" and regime in ["trending_up", "transitional"]:
                    entry = price
                    sl = entry - (30 * pip_size)  # SL 30 pips
                    tp = entry + (60 * pip_size)  # TP 60 pips (RR 2.0)

                    sl_pips = abs(entry - sl) / pip_size
                    tp_pips = abs(tp - entry) / pip_size
                    rr = tp_pips / sl_pips if sl_pips > 0 else 0

                    self.logger.info(
                        f"[LIQUIDITY][{asset}] ⚡ SETUP VALIDE | micro_phase_reversal_buy | "
                        f"Entry={entry:.5f} | SL={sl:.5f} ({sl_pips:.1f}p) | "
                        f"TP={tp:.5f} ({tp_pips:.1f}p) | RR={rr:.2f} | Regime={regime}"
                    )

                    decision = {
                        "asset": asset,
                        "action": "BUY",
                        "entry_price": entry,
                        "sl": sl,
                        "tp": tp,
                        "confidence": 0.60,
                        "rule_name": "liquidity_micro_phase_buy",
                        "strategy_type": "liquidity",
                        "execution_status": "ready",
                        "signals": liquidity_signals,
                        "meta": meta,
                        "rr": rr
                    }

                    self._log_liquidity_consolidated_report(asset, liquidity_signals, decision, price, pip_size)
                    return decision

                # SELL Setup : Micro phase = distribution + Regime favorable
                elif micro_phase == "distribution" and regime in ["trending_down", "transitional"]:
                    entry = price
                    sl = entry + (30 * pip_size)  # SL 30 pips
                    tp = entry - (60 * pip_size)  # TP 60 pips (RR 2.0)

                    sl_pips = abs(sl - entry) / pip_size
                    tp_pips = abs(entry - tp) / pip_size
                    rr = tp_pips / sl_pips if sl_pips > 0 else 0

                    self.logger.info(
                        f"[LIQUIDITY][{asset}] ⚡ SETUP VALIDE | micro_phase_reversal_sell | "
                        f"Entry={entry:.5f} | SL={sl:.5f} ({sl_pips:.1f}p) | "
                        f"TP={tp:.5f} ({tp_pips:.1f}p) | RR={rr:.2f} | Regime={regime}"
                    )

                    decision = {
                        "asset": asset,
                        "action": "SELL",
                        "entry_price": entry,
                        "sl": sl,
                        "tp": tp,
                        "confidence": 0.60,
                        "rule_name": "liquidity_micro_phase_sell",
                        "strategy_type": "liquidity",
                        "execution_status": "ready",
                        "signals": liquidity_signals,
                        "meta": meta,
                        "rr": rr
                    }

                    self._log_liquidity_consolidated_report(asset, liquidity_signals, decision, price, pip_size)
                    return decision

            # --- Aucun setup valide ---
            self.logger.info(f"[DEBUG][{asset}] evaluate_entry terminé → AUCUN setup retenu.")

            # 🔷 Afficher le bilan consolidé liquidity (aucune décision)
            self._log_liquidity_consolidated_report(asset, liquidity_signals, None, price, pip_size)

            return {}

        except Exception as e:
            self.logger.error(f"[{asset}] evaluate_entry error: {e}", exc_info=True)
            return {}


    def _apply_break_even(self, pos: dict, context: dict, rr_threshold: float = 1.0):
        """
        Déplace le SL au prix d'entrée dès que le prix atteint le RR cible.
        rr_threshold = 1.0 => break-even à 1R
        """
        entry = float(pos.get("entry_price") or 0)
        sl = float(pos.get("sl_price") or 0)
        tp = float(pos.get("tp_price") or 0)
        last_price = float(
            context.get("trading_signals", {}).get(pos["symbol"], {}).get("close", 0)
        )

        if not (entry and sl and tp and last_price):
            return None

        # Calcul RR actuel
        risk = abs(entry - sl)
        reward = (
            abs(last_price - entry) if pos.get("type") == 0 else abs(entry - last_price)
        )
        rr_now = reward / risk if risk > 0 else 0

        if rr_now >= rr_threshold and sl != entry:
            return {
                "ticket_to_update": pos["ticket"],
                "new_sl": entry,
                "reason": f"Break-even atteint ({rr_now:.2f}R)",
            }
        return None

    def _apply_trailing_stop(
        self, pos: dict, context: dict, rr_start: float = 1.5, atr_mult: float = 1.2
    ):
        """
        Trailing Stop Liquidity :
        - Active au-delà d'un RR cible (par défaut 1.5R)
        - Place le SL dynamique via ATR (ex: 1.2 * ATR)
        """

        entry = float(pos.get("entry_price") or 0)
        sl = float(pos.get("sl_price") or 0)
        last_price = float(
            context.get("trading_signals", {}).get(pos["symbol"], {}).get("close", 0)
        )

        if not (entry and sl and last_price):
            return None

        # RR actuel
        risk = abs(entry - sl)
        reward = (
            abs(last_price - entry) if pos.get("type") == 0 else abs(entry - last_price)
        )
        rr_now = reward / risk if risk > 0 else 0

        if rr_now < rr_start:
            return None  # trailing pas encore activé

        # Données marché pour ATR
        md = (context.get("market_data") or {}).get(pos["symbol"])
        rates_df = md.get("rates_df") if isinstance(md, dict) else None
        if not isinstance(rates_df, pd.DataFrame) or rates_df.empty:
            return None

        # ATR simple
        high, low, close = rates_df["high"], rates_df["low"], rates_df["close"]
        tr = np.maximum.reduce(
            [
                (high - low).abs(),
                (high - close.shift(1)).abs(),
                (low - close.shift(1)).abs(),
            ]
        )
        atr = tr.rolling(window=14, min_periods=14).mean().iloc[-1]

        if not atr or atr <= 0:
            return None

        # Nouveau SL trailing
        if pos.get("type") == 0:  # BUY
            new_sl = last_price - atr_mult * atr
            if new_sl > sl:
                return {
                    "ticket_to_update": pos["ticket"],
                    "new_sl": new_sl,
                    "reason": "Trailing Stop ATR",
                }
        else:  # SELL
            new_sl = last_price + atr_mult * atr
            if new_sl < sl:
                return {
                    "ticket_to_update": pos["ticket"],
                    "new_sl": new_sl,
                    "reason": "Trailing Stop ATR",
                }

        return None

    def evaluate_exit(
        self, context: Dict[str, Any], current_positions: List[Dict[str, Any]]
    ) -> List[Dict[str, Any]]:
        """
        Évalue les sorties Liquidity (institutionnelles) :
        1. Exit Invalidation : prix retourne dans la zone sweep
        2. Exit Volume : spike de volume à contre-sens
        3. Exit Temps : position ouverte trop longtemps
        4. Exit Rules classiques de la config
        """
        exit_decisions: List[Dict[str, Any]] = []
        cfg_rules = self.strategy_config.get("exit_rules", {})
        liqui_exit_cfg = self.strategy_config.get("liquidity_exit", {})

        # Paramètres depuis la config
        max_bars = int(liqui_exit_cfg.get("max_bars_in_trade", 0) or 0)
        vol_exit_threshold = float(liqui_exit_cfg.get("volume_exit_threshold", 3.0))

        for pos in current_positions:
            if getattr(pos, "magic", None) != self.strategy_config.get("magic_number"):
                continue

            asset = pos.get("symbol")
            asset_sig = context.get("trading_signals", {}).get(asset) or {}
            if not asset_sig:
                continue

            side = "BUY" if pos.get("type") == 0 else "SELL"  # 0=BUY,1=SELL (MT5 conv.)
            last_price = float(asset_sig.get("close", 0.0) or 0.0)

            # === 1) Exit Invalidation : retour dans sweep ===
            sweep_extreme = None
            sd = asset_sig.get("sweep_details")
            if isinstance(sd, dict) and "extreme_price" in sd:
                sweep_extreme = float(sd.get("extreme_price") or 0.0)
            elif isinstance(sd, list) and sd:
                for x in reversed(sd):
                    if (
                        isinstance(x, dict)
                        and x.get("present")
                        and x.get("extreme_price")
                    ):
                        sweep_extreme = float(x["extreme_price"])
                        break

            if sweep_extreme:
                if side == "BUY" and last_price < sweep_extreme:
                    self.logger.info(
                        f"[LIQUIDITY] ⛔ Exit {asset} (BUY) : invalidation sweep (price={last_price:.5f} < sweep={sweep_extreme:.5f})"
                    )
                    exit_decisions.append(
                        {
                            "ticket_to_close": pos["ticket"],
                            "reason": "Invalidation Sweep",
                            "strategy_type": "liquidity",
                            "rule_name": "exit_invalidation",
                        }
                    )

                    continue
                if side == "SELL" and last_price > sweep_extreme:
                    self.logger.info(
                        f"[LIQUIDITY] ⛔ Exit {asset} (SELL) : invalidation sweep (price={last_price:.5f} > sweep={sweep_extreme:.5f})"
                    )
                    exit_decisions.append(
                        {
                            "ticket_to_close": pos["ticket"],
                            "reason": "Invalidation Sweep",
                            "strategy_type": "liquidity",
                            "rule_name": "exit_invalidation",
                        }
                    )

                    continue

            # === 2) Exit Volume : spike anormal contre la position ===
            vol_z = float(asset_sig.get("volume_zscore", 0.0) or 0.0)
            if vol_exit_threshold > 0 and vol_z >= vol_exit_threshold:
                regime = str(asset_sig.get("regime", "")).lower()
                if side == "BUY" and "bear" in regime:
                    self.logger.info(
                        f"[LIQUIDITY] ⛔ Exit {asset} (BUY) : volume spike adverse (vol_z={vol_z:.2f}, regime={regime})"
                    )
                    exit_decisions.append(
                        {
                            "ticket_to_close": pos["ticket"],
                            "reason": "Volume spike adverse",
                        }
                    )
                    continue
                if side == "SELL" and "bull" in regime:
                    self.logger.info(
                        f"[LIQUIDITY] ⛔ Exit {asset} (SELL) : volume spike adverse (vol_z={vol_z:.2f}, regime={regime})"
                    )
                    exit_decisions.append(
                        {
                            "ticket_to_close": pos["ticket"],
                            "reason": "Volume spike adverse",
                        }
                    )
                    continue

                # === 2bis) Trailing Stop institutionnel ===
            ts_decision = self._apply_trailing_stop(
                pos, context, rr_start=1.5, atr_mult=1.2
            )
            if ts_decision:
                exit_decisions.append(ts_decision)
                continue

            # === 3) Exit Temps : position ouverte trop longtemps ===
            if max_bars > 0:
                from datetime import datetime

                try:
                    opened_at = pos.get("open_time")
                    if opened_at:
                        open_dt = datetime.fromisoformat(str(opened_at))
                        now = datetime.utcnow()
                        elapsed_minutes = (now - open_dt).total_seconds() / 60.0
                        bar_size_min = int(
                            self.config_manager.get("execution.bar_size_minutes", 1)
                        )
                        bars_elapsed = int(elapsed_minutes // bar_size_min)
                        if bars_elapsed >= max_bars:
                            self.logger.info(
                                f"[LIQUIDITY] ⏱ Exit {asset} : max bars atteint ({bars_elapsed} >= {max_bars})"
                            )
                            exit_decisions.append(
                                {
                                    "ticket_to_close": pos["ticket"],
                                    "reason": "Max bars in trade atteint",
                                }
                            )
                            continue
                except Exception as e:
                    self.logger.warning(f"[EXIT] Erreur calcul max_bars: {e}")

            # === 4) Exit Rules de la config ===
            if isinstance(cfg_rules, list):
                for rule in cfg_rules:
                    if self._check_rule_conditions(
                        asset_sig, rule.get("conditions", {})
                    ):
                        self.logger.info(
                            f"[LIQUIDITY] ⛔ Exit {asset} par règle config: {rule.get('name', 'Exit Liquidity')}"
                        )
                        exit_decisions.append(
                            {
                                "ticket_to_close": pos["ticket"],
                                "reason": rule.get("name", "Exit Liquidity"),
                            }
                        )
                        break
            elif isinstance(cfg_rules, dict):
                for name, rule in cfg_rules.items():
                    if self._check_rule_conditions(
                        asset_sig, rule.get("conditions", {})
                    ):
                        self.logger.info(
                            f"[LIQUIDITY] ⛔ Exit {asset} par règle config: {name}"
                        )
                        exit_decisions.append(
                            {"ticket_to_close": pos["ticket"], "reason": name}
                        )
                        break

                # === 5) Exit Trailing Stop Liquidity ===
            trail_cfg = liqui_exit_cfg.get("trailing_stop", {})
            if trail_cfg.get("enabled", False):
                atr_period = int(trail_cfg.get("atr_period", 14))
                atr_mult = float(trail_cfg.get("atr_multiplier", 2.0))

                md = (context.get("market_data") or {}).get(asset, {})
                df = md.get("rates_df")
                if isinstance(df, pd.DataFrame) and len(df) >= atr_period + 2:
                    high, low, close = df["high"], df["low"], df["close"]
                    tr = np.maximum.reduce(
                        [
                            (high - low).abs(),
                            (high - close.shift(1)).abs(),
                            (low - close.shift(1)).abs(),
                        ]
                    )
                    atr = tr.rolling(window=atr_period).mean().iloc[-1]

                    if atr and atr > 0:
                        if side == "BUY":
                            new_sl = last_price - atr_mult * atr
                            if new_sl > pos.get("sl", 0):  # seulement si SL monte
                                self.logger.info(
                                    f"[LIQUIDITY] 🔄 Trailing SL BUY {asset}: {pos.get('sl')} -> {new_sl:.5f}"
                                )
                                exit_decisions.append(
                                    {
                                        "ticket_to_close": pos["ticket"],
                                        "reason": "Trailing Stop Update",
                                        "new_sl": new_sl,
                                    }
                                )
                        elif side == "SELL":
                            new_sl = last_price + atr_mult * atr
                            if new_sl < pos.get(
                                "sl", float("inf")
                            ):  # seulement si SL descend
                                self.logger.info(
                                    f"[LIQUIDITY] 🔄 Trailing SL SELL {asset}: {pos.get('sl')} -> {new_sl:.5f}"
                                )
                                exit_decisions.append(
                                    {
                                        "ticket_to_close": pos["ticket"],
                                        "reason": "Trailing Stop Update",
                                        "new_sl": new_sl,
                                    }
                                )
            return exit_decisions

    def get_parameters(self) -> Dict[str, Any]:
        return self.strategy_config.copy()

    def update_strategy_parameters(self, new_params: Dict[str, Any]) -> None:
        self.logger.info(f"Mise à jour des paramètres Liquidity: {new_params}")
        self.strategy_config.update(new_params)
        self.__init__(self.config_manager, self.strategy_config)

    # =========================
    #     INTERNAL HELPERS
    # =========================

    def _build_order_proposal(
        self, asset: str, context: Dict[str, Any], sig: Dict[str, Any]
    ) -> Optional[Dict[str, Any]]:
        """
        Construit la proposition d'ordre Liquidity avec intégration des patterns chandeliers.
        """

        # --- helpers locaux ---
        def _as_float(x, default=0.0) -> float:
            try:
                v = float(x)
                if not (v == v) or v in (float("inf"), float("-inf")):
                    return float(default)
                return v
            except Exception:
                return float(default)

        def _round_to_point(
            price: Optional[float], point_val: float
        ) -> Optional[float]:
            if price is None or price <= 0 or point_val <= 0:
                return price
            steps = round(price / point_val)
            return steps * point_val

        # --- paramètres marché ---
        point = _as_float(sig.get("point", context.get("point", 0.0)), 0.0)
        if point <= 0.0:
            digits = sig.get("digits", context.get("digits"))
            if isinstance(digits, (int, float)) and int(digits) >= 0:
                point = 10.0 ** (-int(digits))
            else:
                point = 0.00001
        pip_size = point * 10.0 if point > 0 else 0.0001
        price_now = _as_float(sig.get("close", context.get("close", 0.0)), 0.0)

        # --- entry_logic ---
        entry_logic = self._cfg_dict("liquidity_sweep.entry_logic", default={})
        trigger = str(entry_logic.get("trigger", "break_of_absorption_extreme")).lower()
        buffer_pips = _as_float(entry_logic.get("buffer_pips", 0.4), 0.4)
        timeout_bars = int(entry_logic.get("timeout_bars", 3))
        direction_pref = str(entry_logic.get("direction", "opposite_of_sweep")).lower()

        exec_cfg = self._cfg_dict("decision_engine.execution", default={})
        order_type = str(exec_cfg.get("order_type", "limit")).upper()
        use_mitigation = bool(exec_cfg.get("limit_use_mitigation", True))

        # --- sens du trade ---
        side = self._infer_direction(sig, direction_pref)
        if side not in ("BUY", "SELL"):
            self.logger.warning("[LIQUIDITY] abandon: direction indécise.")
            return None

        # --- niveaux sweep/absorption ---
        sweep_extreme, absorb_extreme = self._extract_sweep_absorption_extremes(
            sig, side
        )

        # --- entry ---
        entry_price = self._compute_entry_price(
            side=side,
            trigger=trigger,
            buffer_pips=buffer_pips,
            pip_size=pip_size,
            price_now=price_now,
            sweep_extreme=sweep_extreme,
            absorb_extreme=absorb_extreme,
            sig=sig,
            use_mitigation=use_mitigation if order_type == "LIMIT" else False,
        )
        if entry_price is None or entry_price <= 0:
            self.logger.warning("[LIQUIDITY] abandon: entry_price invalide.")
            return None
        entry_price = _round_to_point(entry_price, point)

        # --- SL ---
        sl_price = self._compute_sl(
            side=side, pip_size=pip_size, sweep_extreme=sweep_extreme, sig=sig
        )
        if sl_price is None or sl_price <= 0:
            self.logger.warning("[LIQUIDITY] abandon: sl_price invalide.")
            return None
        sl_price = _round_to_point(sl_price, point)
        if sl_price == entry_price:
            self.logger.warning("[LIQUIDITY] abandon: sl_price == entry_price.")
            return None

        # --- TP ---
        tp_price = self._compute_tp(
            asset=asset, side=side, entry_price=entry_price, pip_size=pip_size, sig=sig
        )
        if tp_price is not None and tp_price > 0:
            tp_price = _round_to_point(tp_price, point)

        # --- RR ---
        rr_est: Optional[float] = None
        try:
            rr_est = self._estimate_rr(entry_price, sl_price, tp_price, side)
        except Exception:
            rr_est = None

        # --- Base proposal ---
        confidence = float(sig.get("confidence_score", 0.0))
        rule_name = "liquidity_sweep_absorption"

        # === 🔥 Patch PatternEngine ===
        latest_pat = sig.get("latest_pattern")
        if latest_pat:
            pat_name = str(latest_pat.get("pattern", "")).lower()
            pat_bull = latest_pat.get("is_bullish", None)

            # Ajuster confiance selon confluence pattern
            if side == "BUY" and pat_bull is True:
                confidence += 0.2
            elif side == "SELL" and pat_bull is False:
                confidence += 0.2
            elif pat_bull is not None:
                confidence -= 0.1  # contradiction légère

            rule_name += f"+pattern:{pat_name}"

        proposal = {
            "action": side,
            "entry_price": entry_price,
            "sl_price": sl_price,
            "tp_price": tp_price,
            "order_type": order_type,
            "buffer_pips": buffer_pips,
            "timeout_bars": timeout_bars,
            "rr_estimate": rr_est,
            "use_mitigation": use_mitigation if order_type == "LIMIT" else False,
            "confidence": round(confidence, 3),
            "rule_name": rule_name,
            "direction_pref": direction_pref,
        }

        self.logger.info(
            f"[LIQUIDITY] ✅ Proposition: side={side} entry={proposal['entry_price']} "
            f"sl={proposal['sl_price']} tp={proposal['tp_price']} rr≈{proposal['rr_estimate']} "
            f"| conf={proposal['confidence']}"
        )
        return proposal

    def _infer_direction(
        self,
        sig: Dict[str, Any],
        direction_pref: Optional[str] = None,
    ) -> Optional[str]:
        """
        Détermine la direction (BUY/SELL) d'un setup Liquidity.

        Priorité par défaut (mode 'auto'):
        1) Sweep (low → BUY, high → SELL)  [opposé au sweep]
        2) Validation MTF bias (si présent et aligné)
        3) Phase/Régime (fallback)
        → Si incertain ou conflit: None

        Paramètre optionnel:
        - direction_pref (str): peut forcer une heuristique particulière.
            Valeurs supportées (insensibles à la casse):
            * "opposite_of_sweep" (défaut) : low→BUY, high→SELL
            * "with_sweep" / "follow_sweep" : low→SELL, high→BUY
            * "mtf_bias" : utiliser exclusivement le biais MTF (si aligné)
            * "phase" / "regime" : utiliser le contexte de phase/régime
            * "auto" : suit la priorité par défaut ci-dessus
        """
        # -- normalisation
        pref = (direction_pref or "auto").strip().lower()
        side: Optional[str] = None

        # === Helpers locaux ===
        def _sweep_side(opposite: bool = True) -> Optional[str]:
            sd = sig.get("sweep_details")
            sweep_type = None

            # sweep_details peut être dict (dernier) ou liste (historique)
            if isinstance(sd, dict) and sd.get("present"):
                sweep_type = str(sd.get("sweep_type", "")).lower()
            elif isinstance(sd, list) and sd:
                for x in reversed(sd):
                    if isinstance(x, dict) and x.get("present"):
                        sweep_type = str(x.get("sweep_type", "")).lower()
                        break

            if sweep_type is None:
                return None

            # mapping
            if opposite:
                # Opposé au sweep : low→BUY, high→SELL
                return (
                    "BUY"
                    if sweep_type == "low"
                    else ("SELL" if sweep_type == "high" else None)
                )
            else:
                # Avec le sweep : low→SELL, high→BUY
                return (
                    "SELL"
                    if sweep_type == "low"
                    else ("BUY" if sweep_type == "high" else None)
                )

        def _mtf_direction() -> Tuple[Optional[str], bool]:
            mtf_bias = sig.get("mtf_bias")
            mtf_aligned = bool(sig.get("mtf_bias_aligned", True))
            if not mtf_aligned:
                return None, False
            if mtf_bias:
                mb = str(mtf_bias).upper()
                if mb in ("BUY", "SELL"):
                    return mb, True
            return None, True  # aligné mais pas de biais exploitable

        def _phase_regime_direction() -> Optional[str]:
            phase = str(sig.get("phase", "")).lower()
            regime = str(sig.get("regime", "")).lower()
            # Heuristique simple : si momentum/impulsion détecté, utiliser le polarity du regime
            if "impulse" in phase or "impulsion" in regime or "momentum" in regime:
                if "bull" in regime:
                    return "BUY"
                if "bear" in regime:
                    return "SELL"
            return None

        # === Modes orientés par préférence explicite ===
        if pref in ("with_sweep", "follow_sweep"):
            # direction = avec le sweep (utile pour stratégies breakout)
            side = _sweep_side(opposite=False)
            if side is None:
                self.logger.warning(
                    "[LIQUIDITY] ❌ Direction with_sweep impossible: sweep absent."
                )
                return None

            mtf_side, mtf_ok = _mtf_direction()
            if not mtf_ok:
                self.logger.warning(
                    "[LIQUIDITY] ❌ Direction rejetée : MTF bias non aligné."
                )
                return None
            if mtf_side and mtf_side != side:
                self.logger.warning(
                    f"[LIQUIDITY] ❌ Conflit directionnel with_sweep: side={side}, mtf_bias={mtf_side}"
                )
                return None
            self.logger.info(f"[LIQUIDITY] ✅ Direction with_sweep confirmée: {side}")
            return side

        if pref in ("mtf_bias", "mtf"):
            side_mtf, mtf_ok = _mtf_direction()
            if not mtf_ok:
                self.logger.warning(
                    "[LIQUIDITY] ❌ Direction rejetée : MTF bias non aligné."
                )
                return None
            if side_mtf in ("BUY", "SELL"):
                self.logger.info(f"[LIQUIDITY] ✅ Direction (mtf_bias): {side_mtf}")
                return side_mtf
            self.logger.warning(
                "[LIQUIDITY] ❌ Direction (mtf_bias) indécise (biais absent)."
            )
            return None

        if pref in ("phase", "regime"):
            side_phase = _phase_regime_direction()
            if side_phase in ("BUY", "SELL"):
                # Vérifier alignement MTF s'il existe
                side_mtf, mtf_ok = _mtf_direction()
                if not mtf_ok:
                    self.logger.warning(
                        "[LIQUIDITY] ❌ Direction rejetée : MTF bias non aligné."
                    )
                    return None
                if side_mtf and side_mtf != side_phase:
                    self.logger.warning(
                        f"[LIQUIDITY] ❌ Conflit directionnel phase: side={side_phase}, mtf_bias={side_mtf}"
                    )
                    return None
                self.logger.info(
                    f"[LIQUIDITY] ✅ Direction (phase/regime): {side_phase}"
                )
                return side_phase
            self.logger.warning("[LIQUIDITY] ❌ Direction (phase/regime) indécise.")
            return None

        # === Mode 'auto' (ou 'opposite_of_sweep' explicite) ===
        # 1) Sweep (opposé par défaut)  2) MTF  3) Phase/Régime
        if pref in ("auto", "opposite_of_sweep", "opposite", ""):
            side = _sweep_side(opposite=True)
            side_mtf, mtf_ok = _mtf_direction()

            if not mtf_ok:
                self.logger.warning(
                    "[LIQUIDITY] ❌ Direction rejetée : MTF bias non aligné."
                )
                return None

            if side:
                if side_mtf and side_mtf != side:
                    self.logger.warning(
                        f"[LIQUIDITY] ❌ Conflit directionnel: sweep_side={side}, mtf_bias={side_mtf}"
                    )
                    return None
                self.logger.info(f"[LIQUIDITY] ✅ Direction (sweep→opposé): {side}")
                return side

            if side_mtf in ("BUY", "SELL"):
                self.logger.info(f"[LIQUIDITY] ✅ Direction (mtf_bias): {side_mtf}")
                return side_mtf

            side_phase = _phase_regime_direction()
            if side_phase in ("BUY", "SELL"):
                self.logger.info(
                    f"[LIQUIDITY] ✅ Direction (phase/regime): {side_phase}"
                )
                return side_phase

            self.logger.warning(
                "[LIQUIDITY] ❌ Direction indécise (aucun sweep/MTF/phase)."
            )
            return None

        # === Préférence non reconnue → fallback auto
        self.logger.debug(
            f"[LIQUIDITY] ⚠️ direction_pref inconnu '{direction_pref}', fallback 'auto'."
        )
        return self._infer_direction(sig, direction_pref="auto")

    def _last_sweep_side(self, sig: Dict[str, Any]) -> Optional[str]:
        """
        Retourne 'buy' (sweep down) ou 'sell' (sweep up) si disponible dans sweep_details.
        sweep_details peut être:
        - une liste alignée sur les barres (dict ou None)
        - un dict 'last' avec 'side'
        """
        sd = sig.get("sweep_details")
        if isinstance(sd, dict) and "side" in sd:
            # format compact
            return str(sd.get("side")).lower()
        if isinstance(sd, list) and len(sd) > 0:
            # chercher le dernier dict non None
            for x in reversed(sd):
                if isinstance(x, dict) and x.get("side"):
                    return str(x.get("side")).lower()
        return None

    def _extract_sweep_absorption_extremes(
        self, sig: Dict[str, Any], side: str
    ) -> Tuple[Optional[float], Optional[float]]:
        """
        Déduit les extrêmes utiles :
        - sweep_extreme: le HH/LL sweepé (référence SL)
        - absorb_extreme: l'extrême de la/les bougies d'absorption (référence pour break entry)
        """
        sweep_extreme = None
        sd = sig.get("sweep_details")
        if isinstance(sd, dict):
            sweep_extreme = float(sd.get("extreme_price", 0.0) or 0.0) or None
        elif isinstance(sd, list) and len(sd) > 0:
            for x in reversed(sd):
                if isinstance(x, dict) and x.get("present"):
                    # pas toujours fourni; on tente 'extreme_price' sinon None
                    val = x.get("extreme_price")
                    if val is not None:
                        sweep_extreme = float(val)
                        break

        absorb_extreme = None
        ad = sig.get("absorption_details")
        if isinstance(ad, dict):
            absorb_extreme = float(ad.get("extreme_price", 0.0) or 0.0) or None
        elif isinstance(ad, list) and len(ad) > 0:
            for x in reversed(ad):
                if isinstance(x, dict) and x.get("confirmed"):
                    val_h = x.get("high")
                    val_l = x.get("low")
                    if side == "BUY" and val_h is not None:
                        absorb_extreme = float(val_h)
                        break
                    if side == "SELL" and val_l is not None:
                        absorb_extreme = float(val_l)
                        break

        return sweep_extreme, absorb_extreme

    def _compute_entry_price(
        self,
        side: str,
        trigger: str,
        buffer_pips: float,
        pip_size: float,
        price_now: float,
        sweep_extreme: Optional[float],
        absorb_extreme: Optional[float],
        sig: Dict[str, Any],
        use_mitigation: bool,
    ) -> Optional[float]:
        """
        ENTRY LOGIC (STRICT, no fallback):
        - break_of_absorption_extreme ± buffer  → nécessite absorb_extreme
        - retracement: priorité OB∩FVG (confluence), sinon OB puis FVG → si aucune zone valide: None
        - wick_fill: entrée à X% du remplissage de mèche (nécessite sweep_extreme & absorb_extreme)
        - support MTF si des zones HTF sont exposées dans les signaux
        - aucune dégradation vers un 'break' si le retracement échoue
        """

        # --- Config lecture (avec défauts sûrs) ---
        el_cfg = self._cfg_dict("liquidity_sweep.entry_logic", {})
        prefer_confluent = bool(el_cfg.get("prefer_confluent_zone", True))
        use_wick_fill = bool(el_cfg.get("use_wick_fill", False))
        wick_fill_ratio = float(el_cfg.get("wick_fill_ratio", 0.50))  # 0..1
        use_mtf_zone = bool(el_cfg.get("use_mtf_zone", False))
        retr_tolerance_pips = float(el_cfg.get("retracement_tolerance_pips", 3.0))
        retr_tolerance_px = max(0.0, retr_tolerance_pips) * max(pip_size, 0.0)
        mitigation_ratio = float(el_cfg.get("mitigation_ratio", 0.33))

        # Buffer en prix
        buf = float(buffer_pips) * max(pip_size, 0.0)

        # Helpers locaux pour lire une "zone"
        def zone_band_from_dict(d: Dict[str, Any]) -> Optional[Tuple[float, float]]:
            if not isinstance(d, dict):
                return None
            keys_hi = ("zone_high", "upper", "high", "hi")
            keys_lo = ("zone_low", "lower", "low", "lo")
            z_hi = None
            z_lo = None
            for k in keys_hi:
                if d.get(k) is not None:
                    try:
                        z_hi = float(d[k])
                        break
                    except Exception:
                        pass
            for k in keys_lo:
                if d.get(k) is not None:
                    try:
                        z_lo = float(d[k])
                        break
                    except Exception:
                        pass
            if z_hi is None and z_lo is not None:
                z_hi = z_lo
            if z_lo is None and z_hi is not None:
                z_lo = z_hi
            if z_lo is None or z_hi is None:
                return None
            lo, hi = (min(z_lo, z_hi), max(z_lo, z_hi))
            return (lo, hi)

        def zone_band_from_obj(obj: Any) -> Optional[Tuple[float, float]]:
            if isinstance(obj, dict):
                return zone_band_from_dict(obj)
            if isinstance(obj, list) and obj:
                for x in reversed(obj):
                    zb = zone_band_from_dict(x) if isinstance(x, dict) else None
                    if zb:
                        return zb
            return None

        def zone_overlap(
            a: Tuple[float, float], b: Tuple[float, float]
        ) -> Optional[Tuple[float, float]]:
            lo = max(a[0], b[0])
            hi = min(a[1], b[1])
            return (lo, hi) if hi >= lo else None

        def edge_or_mitigated(band: Tuple[float, float], side_: str) -> float:
            lo, hi = band
            if not use_mitigation:
                return lo if side_ == "BUY" else hi
            width = max(0.0, hi - lo)
            if width <= 0.0:
                return lo if side_ == "BUY" else hi
            return (
                (lo + mitigation_ratio * width)
                if side_ == "BUY"
                else (hi - mitigation_ratio * width)
            )

        def nearest_valid_retracement(
            side_: str, candidates: List[float], ref_price: float
        ) -> Optional[float]:
            if side_ == "BUY":
                cands = [c for c in candidates if c <= ref_price + retr_tolerance_px]
                if not cands:
                    return None
                cands.sort(key=lambda x: (abs(ref_price - x), -x))
                return cands[0]
            else:
                cands = [c for c in candidates if c >= ref_price - retr_tolerance_px]
                if not cands:
                    return None
                cands.sort(key=lambda x: (abs(x - ref_price), x))
                return cands[0]

        # --- 0) Wick Fill (optionnel, strict: nécessite sweep_extreme & absorb_extreme) ---
        if (
            use_wick_fill
            and sweep_extreme is not None
            and absorb_extreme is not None
            and 0.0 <= wick_fill_ratio <= 1.0
        ):
            try:
                anchor = float(sweep_extreme)
                target = float(absorb_extreme)
                if side == "BUY":
                    entry_wick = min(
                        anchor + wick_fill_ratio * (target - anchor) + buf, target
                    )
                else:
                    entry_wick = max(
                        anchor - wick_fill_ratio * (anchor - target) - buf, target
                    )
                if entry_wick > 0:
                    return round(float(entry_wick), 10)
            except Exception:
                pass  # on retombe sur la logique stricte ci-dessous

        # --- 1) BREAK (STRICT) ---
        if trigger == "break_of_absorption_extreme":
            if absorb_extreme is None:
                self.logger.warning(
                    "[LIQUIDITY] STRICT: break_of_absorption_extreme sans absorb_extreme → None"
                )
                return None
            ref = float(absorb_extreme)
            price = (ref + buf) if side == "BUY" else (ref - buf)
            return round(price, 10) if price > 0 else None

        # --- 2) RETRACEMENT (STRICT : aucune dégradation en break si pas de zone valide) ---
        if trigger in {"retracement", "ob_or_fvg_retest", "retest"}:
            ob_band = zone_band_from_obj(sig.get("ob_details"))
            fvg_band = zone_band_from_obj(sig.get("fvg_details"))
            if use_mtf_zone:
                ob_band_htf = zone_band_from_obj(
                    sig.get("ob_details_htf")
                    or sig.get("ob_mtf")
                    or sig.get("ob_higher_tf")
                )
                fvg_band_htf = zone_band_from_obj(
                    sig.get("fvg_details_htf")
                    or sig.get("fvg_mtf")
                    or sig.get("fvg_higher_tf")
                )
            else:
                ob_band_htf = None
                fvg_band_htf = None

            confl_band = None
            if prefer_confluent and ob_band and fvg_band:
                confl_band = zone_overlap(ob_band, fvg_band)
            if prefer_confluent and confl_band is None and ob_band_htf and fvg_band_htf:
                confl_band = zone_overlap(ob_band_htf, fvg_band_htf)

            candidates_prices: List[float] = []
            if confl_band:
                candidates_prices.append(edge_or_mitigated(confl_band, side))
            if ob_band:
                candidates_prices.append(edge_or_mitigated(ob_band, side))
            if fvg_band:
                candidates_prices.append(edge_or_mitigated(fvg_band, side))
            if use_mtf_zone:
                if ob_band_htf:
                    candidates_prices.append(edge_or_mitigated(ob_band_htf, side))
                if fvg_band_htf:
                    candidates_prices.append(edge_or_mitigated(fvg_band_htf, side))

            level = nearest_valid_retracement(side, candidates_prices, float(price_now))
            if level is not None and level > 0:
                return round(float(level), 10)

            # STRICT: pas de fallback en break si aucun niveau de retracement valide
            self.logger.info("[LIQUIDITY] STRICT: retracement sans zone valide → None")
            return None

        # --- 3) Triggers inconnus : STRICT → None ---
        self.logger.warning(f"[LIQUIDITY] STRICT: trigger inconnu '{trigger}' → None")
        return None

    def _first_retracement_level(
        self, sig: Dict[str, Any], side: str
    ) -> Optional[float]:
        """
        Récupère un niveau de retracement pertinent (OB > FVG).
        On lit ob_details / fvg_details (format dict ou liste).
        Retourne le prix 'limite' à tester.
        """
        # OB prioritaire
        ob = sig.get("ob_details")
        price = self._extract_zone_price(ob, side)
        if price is not None:
            return price

        # FVG ensuite
        fvg = sig.get("fvg_details")
        price = self._extract_zone_price(fvg, side)
        if price is not None:
            return price

        return None

    def _extract_zone_price(self, obj: Any, side: str) -> Optional[float]:
        """
        Essaie d'extraire un prix 'limite' d'une zone (OB/FVG), en prenant:
        - pour BUY: bord inférieur de la zone
        - pour SELL: bord supérieur de la zone
        Supporte dict ou liste de dicts.
        """
        if isinstance(obj, dict):
            return self._zone_edge_from_dict(obj, side)
        if isinstance(obj, list) and len(obj) > 0:
            # on essaye le dernier/plus récent
            for x in reversed(obj):
                if isinstance(x, dict):
                    val = self._zone_edge_from_dict(x, side)
                    if val is not None:
                        return val
        return None

    def _zone_edge_from_dict(self, d: Dict[str, Any], side: str) -> Optional[float]:
        """
        Cherche 'zone_high'/'zone_low' ou 'upper'/'lower' dans un dict.
        """
        keys_hi = ("zone_high", "upper", "high", "hi")
        keys_lo = ("zone_low", "lower", "low", "lo")
        z_hi = None
        z_lo = None
        for k in keys_hi:
            if k in d and d[k] is not None:
                z_hi = float(d[k])
                break
        for k in keys_lo:
            if k in d and d[k] is not None:
                z_lo = float(d[k])
                break
        if side == "BUY" and z_lo is not None:
            return z_lo
        if side == "SELL" and z_hi is not None:
            return z_hi
        return None

    def _compute_sl(
        self,
        side: str,
        sig: Dict[str, Any],
        entry_price: float,
        buffer_pips: float = 2.0,
    ) -> Optional[float]:
        """
        Stop-loss institutionnel (strict Liquidity):
        - SL initial = extrême du sweep ± buffer
        - Trailing structurel activé si config l’autorise :
            • BUY: sous dernier HL ou OB valide
            • SELL: au-dessus dernier LH ou OB valide
        - ❌ Aucun fallback ATR : si pas de niveau Liquidity → retourne None
        """
        try:
            pip_size = float(sig.get("point", 0.0001)) * 10.0
        except Exception:
            pip_size = 0.0001
        buf = buffer_pips * pip_size

        sl_price = None

        # === 1) Sweep extrême ===
        sweep_extreme = None
        sd = sig.get("sweep_details")
        if isinstance(sd, dict) and "extreme_price" in sd:
            sweep_extreme = float(sd["extreme_price"])
        elif isinstance(sd, list):
            for x in reversed(sd):
                if isinstance(x, dict) and x.get("present") and x.get("extreme_price"):
                    sweep_extreme = float(x["extreme_price"])
                    break

        if sweep_extreme:
            if side == "BUY":
                sl_price = sweep_extreme - buf
            elif side == "SELL":
                sl_price = sweep_extreme + buf

        # === 2) Trailing structurel (si activé en config) ===
        trailing_cfg = self._cfg_dict("liquidity_exit.trailing_structure", {})
        if trailing_cfg.get("enabled", False):
            # BOS/MSS
            bos = sig.get("bos_mss_details")
            if isinstance(bos, dict) and bos.get("swing_point"):
                bos_level = float(bos.get("swing_point"))
                if side == "BUY" and bos_level and bos_level > 0:
                    sl_price = max(sl_price or 0, bos_level - buf)
                elif side == "SELL" and bos_level and bos_level > 0:
                    sl_price = min(sl_price or 1e9, bos_level + buf)

            # Order Block
            ob = sig.get("ob_details")
            if isinstance(ob, dict):
                if side == "BUY" and ob.get("zone_low"):
                    sl_price = max(sl_price or 0, float(ob["zone_low"]) - buf)
                elif side == "SELL" and ob.get("zone_high"):
                    sl_price = min(sl_price or 1e9, float(ob["zone_high"]) + buf)

        # === Résultat strict ===
        if not sl_price:
            self.logger.warning(
                "[LIQUIDITY] ❌ Aucun SL Liquidity identifié (sweep/OB/BOS)."
            )
            return None

        self.logger.info(f"[LIQUIDITY] 🛡️ Stop-loss défini: {sl_price:.5f}")
        return round(sl_price, 10)

    def _compute_tp(
        self,
        side: str,
        sig: Dict[str, Any],
        entry_price: float,
        sl_price: float,
        confidence: float = 0.7,
    ) -> Optional[List[float]]:
        """
        TP institutionnel (strict Liquidity) :
        - TP1 = cluster de liquidité le plus proche (EQH/EQL > OB > FVG)
        - TP2 = niveau plus éloigné (2e cluster, OB ou FVG suivant)
        - ❌ Aucun fallback RR : si pas de cible Liquidity → retourne None
        """
        try:
            pip_size = float(sig.get("point", 0.0001)) * 10.0
        except Exception:
            pip_size = 0.0001

        candidates = []

        # === EQH/EQL ===
        eqh_eql = sig.get("eqh_eql_details")
        if isinstance(eqh_eql, list) and eqh_eql:
            for lvl in eqh_eql:
                if not isinstance(lvl, dict):
                    continue
                price = lvl.get("level_price")
                direction = str(lvl.get("direction", "")).lower()
                if not price:
                    continue
                if side == "BUY" and "high" in direction:
                    candidates.append(("EQH", float(price)))
                elif side == "SELL" and "low" in direction:
                    candidates.append(("EQL", float(price)))

        # === OB ===
        ob = sig.get("ob_details")
        if ob and isinstance(ob, dict):
            lvl = ob.get("zone_high") if side == "BUY" else ob.get("zone_low")
            if lvl:
                candidates.append(("OB", float(lvl)))

        # === FVG ===
        fvg = sig.get("fvg_details")
        if fvg and isinstance(fvg, dict):
            lvl = fvg.get("zone_high") if side == "BUY" else fvg.get("zone_low")
            if lvl:
                candidates.append(("FVG", float(lvl)))

        # === Filtrage directionnel & tri ===
        if side == "BUY":
            candidates = [(tag, lvl) for tag, lvl in candidates if lvl > entry_price]
            candidates.sort(key=lambda x: x[1])  # plus proche en premier
        else:
            candidates = [(tag, lvl) for tag, lvl in candidates if lvl < entry_price]
            candidates.sort(key=lambda x: x[1], reverse=True)

        # === Résultat strict ===
        if not candidates:
            self.logger.warning(
                "[LIQUIDITY] ❌ Aucun TP Liquidity identifié (EQH/EQL, OB, FVG)."
            )
            return None

        tp1 = candidates[0][1]  # cible la plus proche
        tp2 = candidates[1][1] if len(candidates) > 1 else None

        tp_list = [round(tp1, 10)]
        if tp2:
            tp_list.append(round(tp2, 10))

        self.logger.info(f"[LIQUIDITY] 🎯 TP Liquidity choisis: {tp_list}")
        return tp_list

    def _nearest_liquidity_level(
        self, side: str, sig: Dict[str, Any]
    ) -> Optional[float]:
        """
        Sélectionne la cible de liquidité la plus pertinente dans la direction du trade.
        Priorité:
          1. Cluster EQH/EQL
          2. Order Block
          3. Fair Value Gap
        Fallback: None (la stratégie utilisera smart_targets ATR si rien trouvé).
        """
        candidates = []

        # === 1) EQH / EQL (clusters)
        eqh_eql = sig.get("eqh_eql_details")
        if isinstance(eqh_eql, list) and eqh_eql:
            levels = []
            for lvl in eqh_eql:
                if not isinstance(lvl, dict):
                    continue
                price = lvl.get("level_price")
                if not price:
                    continue
                direction = lvl.get("direction", "").lower()
                if (side == "BUY" and "high" in direction) or (
                    side == "SELL" and "low" in direction
                ):
                    levels.append(float(price))

            if levels:
                # cluster = moyenne des niveaux proches (< 5 pips d’écart)
                pip_size = float(sig.get("point", 0.0001)) * 10.0
                levels = sorted(levels)
                cluster = [levels[0]]
                for p in levels[1:]:
                    if abs(p - cluster[-1]) <= 5 * pip_size:
                        cluster.append(p)
                    else:
                        break
                cluster_level = sum(cluster) / len(cluster)
                candidates.append(("EQH/EQL", cluster_level))

        # === 2) Order Block
        ob = sig.get("ob_details")
        lvl = self._zone_target(side, ob)
        if lvl:
            candidates.append(("OB", lvl))

        # === 3) Fair Value Gap
        fvg = sig.get("fvg_details")
        lvl = self._zone_target(side, fvg)
        if lvl:
            candidates.append(("FVG", lvl))

        if not candidates:
            return None

        # --- Tri par priorité (EQH/EQL > OB > FVG) ---
        priority = {"EQH/EQL": 0, "OB": 1, "FVG": 2}
        candidates.sort(key=lambda x: priority.get(x[0], 99))

        best = candidates[0]
        self.logger.info(f"[LIQUIDITY] 🎯 Cible choisie ({best[0]}): {best[1]:.5f}")
        return best[1]

    def _eqh_eql_target(self, side: str, eqh_eql) -> Optional[float]:
        """
        Cherche un niveau EQH/EQL exploitable côté cible.
        eqh_eql peut être dict ou liste; retourne un niveau (float) si pertinent.
        - BUY: viser un EQH (liquidité au-dessus)
        - SELL: viser un EQL (liquidité en-dessous)
        """
        target_type = "eqh" if side == "BUY" else "eql"
        if isinstance(eqh_eql, dict):
            if str(eqh_eql.get("type", "")).lower() == target_type and eqh_eql.get(
                "level"
            ):
                return float(eqh_eql["level"])
        if isinstance(eqh_eql, list) and len(eqh_eql) > 0:
            for x in reversed(eqh_eql):
                if (
                    isinstance(x, dict)
                    and str(x.get("type", "")).lower() == target_type
                    and x.get("level")
                ):
                    return float(x["level"])
        return None

    def _zone_target(self, side: str, obj) -> Optional[float]:
        """
        Retourne un bord de zone (OB/FVG) dans la direction TP.
        BUY -> viser bord supérieur ; SELL -> bord inférieur (on va chercher la liquidité opposée).
        """
        if isinstance(obj, dict):
            return self._zone_tp_from_dict(obj, side)
        if isinstance(obj, list) and len(obj) > 0:
            for x in reversed(obj):
                if isinstance(x, dict):
                    val = self._zone_tp_from_dict(x, side)
                    if val is not None:
                        return val
        return None

    def _zone_tp_from_dict(self, d: Dict[str, Any], side: str) -> Optional[float]:
        keys_hi = ("zone_high", "upper", "high", "hi")
        keys_lo = ("zone_low", "lower", "low", "lo")
        z_hi = None
        z_lo = None
        for k in keys_hi:
            if k in d and d[k] is not None:
                z_hi = float(d[k])
                break
        for k in keys_lo:
            if k in d and d[k] is not None:
                z_lo = float(d[k])
                break
        if side == "BUY" and z_hi is not None:
            return z_hi
        if side == "SELL" and z_lo is not None:
            return z_lo
        return None

    def _extract_atr(
        self, sig: Dict[str, Any], period: int, pip_size: float
    ) -> Optional[float]:
        """
        Extrait (ou approxime) un ATR en prix.
        - Si sig fournit 'atr_pips' (ex: via bollinger.atr_pips): convertit en prix
        - Sinon essaye 'atr_value' déjà en prix
        """
        atr_pips = sig.get("atr_pips")
        if atr_pips is not None:
            try:
                return float(atr_pips) * pip_size
            except Exception:
                pass
        atr_val = sig.get("atr_value")
        if atr_val is not None:
            try:
                return float(atr_val)
            except Exception:
                pass
        return None

    def _estimate_rr(
        self,
        entry_price: float,
        sl_price: float,
        tp_prices: Optional[List[float]],
        sig: Dict[str, Any],
        confidence: float = 0.7,
    ) -> Optional[float]:
        """
        Estime le RR institutionnel (strict Liquidity).
        - RR = |TP - Entry| / |Entry - SL|
        - Pondéré par confiance et confluence
        - ❌ Aucun fallback si TP/SL manquant
        """
        if not entry_price or not sl_price or not tp_prices:
            self.logger.warning(
                "[LIQUIDITY] ❌ Impossible de calculer RR (entry/sl/tp manquant)."
            )
            return None

        try:
            # Distance risque (SL)
            risk = abs(entry_price - sl_price)
            if risk <= 0:
                return None

            # Distance reward (on prend TP le plus éloigné)
            reward = max(abs(tp - entry_price) for tp in tp_prices if tp)
            if reward <= 0:
                return None

            rr = reward / risk

            # --- Pondération par confiance ---
            if confidence < 0.6:
                rr *= 0.7
            elif confidence > 0.8:
                rr *= 1.1

            # --- Bonus confluence ---
            fvg = bool(sig.get("fvg_detected"))
            ob = bool(sig.get("ob_detected"))
            bos = bool(sig.get("bos_mss_detected"))
            if fvg and ob and bos:
                rr *= 1.1

            rr = round(rr, 2)
            self.logger.info(f"[LIQUIDITY] 📊 RR estimé: {rr}")
            return rr

        except Exception as e:
            self.logger.warning(f"[LIQUIDITY] Erreur calcul RR: {e}")
            return None

    # =========================
    #  DECISION PACKAGE (OUT)
    # =========================
    def _build_decision_package_from_proposal(
        self, asset: str, p: Dict[str, Any]
    ) -> Dict[str, Any]:
        """
        Construit un package d'ordre complet pour l'executor.
        On inclut à la fois prix absolus et fallback pips (compat descendante).
        """
        # fallback pips (au cas où l’executor en aurait besoin)
        target_tp_pips = None
        target_sl_pips = None
        try:
            if p.get("tp_price") and p.get("entry_price"):
                target_tp_pips = abs(p["tp_price"] - p["entry_price"])
            if p.get("sl_price") and p.get("entry_price"):
                target_sl_pips = abs(p["entry_price"] - p["sl_price"])
        except Exception:
            pass

        # Rule name plus souple (si défini dans la proposition, sinon valeur par défaut)
        rule_name = p.get("rule_name") or "liquidity_entry"

        decision = {
            "action": p.get("action"),
            "asset": asset,
            "order_type": p.get("order_type", "LIMIT"),
            "strategy_type": self.strategy_config.get("strategy_name", "liquidity"),
            "rule_name": rule_name,
            "magic_number": self.strategy_config.get("magic_number"),
            # Params d'exécution
            "entry_price": p.get("entry_price"),
            "sl_price": p.get("sl_price"),
            "tp_price": p.get("tp_price"),
            "timeout_bars": p.get("timeout_bars"),
            "use_mitigation": p.get("use_mitigation"),
            # Compat descendante (si l'executor attend encore des 'pips')
            "target_tp_pips": target_tp_pips,
            "target_sl_pips": target_sl_pips,
            # Métadonnées utiles pour logs et audit
            "confidence": p.get("confidence", 0.0),
        }
        return decision

    # =========================
    #     CONFIG HELPERS
    # =========================
    def _cfg(self, path: str, default: Any = None) -> Any:
        """Lit un scalaire depuis strategy_config ou config_manager."""
        # 1) essayer strategy_config (chemin 'a.b.c')
        cur = self.strategy_config
        for part in path.split("."):
            if not isinstance(cur, dict) or part not in cur:
                cur = None
                break
            cur = cur[part]
        if cur is not None:
            return cur

        # 2) sinon config_manager global
        try:
            if self.config_manager:
                val = self.config_manager.get(path, default)
                return default if val is None else val
        except Exception:
            pass
        return default

    def _cfg_dict(
        self, path: str, default: Optional[Dict[str, Any]] = None
    ) -> Dict[str, Any]:
        val = self._cfg(path, default=default or {})
        return val if isinstance(val, dict) else (default or {})

    def _cfg_list(self, path: str, default: Optional[List[Any]] = None) -> List[Any]:
        val = self._cfg(path, default=default or [])
        return val if isinstance(val, list) else (default or [])
