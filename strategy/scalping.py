# scalping.py — version Burst-Only (no Bollinger / no Katana)
from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple
import math
import time
import uuid
from .base_strategy import BaseStrategy
import numpy as np
import pandas as pd
from phase_observer.detectors import Detectors
from phase_observer.market_analyzer import MarketAnalyzer



class ScalpingStrategy(BaseStrategy):
    """
    Stratégie SCALPING focalisée sur :
      1) Burst Scalping (basket d’ordres simultanés) — priorité
      2) Liquidity Sweep (cassures HH/LL récentes) — optionnel

    ➤ Zéro dépendance Bollinger/midline/Katana.
    ➤ Les tailles (volume) sont déléguées au TradeExecutor (risk-based).
    ➤ Les SL/TP peuvent être fournis en pips (convertis en prix) ou
       laissés au moteur de SL/TP du TradeExecutor (_calculate_sl_tp_prices).
    """

    def __init__(
        self,
        config_manager,
        strategy_config: Optional[Dict[str, Any]] = None,
        mt5_connector=None,   # 👈 ajouté ici
        logger=None,
    ):
        """
        Initialise la stratégie Scalping.
        """
        super().__init__(config_manager, strategy_config or {})

        self.config_manager = config_manager
        self.strategy_config = strategy_config or {}
        self.mt5_connector = mt5_connector   # ✅ plus d'erreur
        self.logger = logger or getattr(config_manager, "logger", None)
        self.market_analyzer = MarketAnalyzer(
            config_manager=self.config_manager,
            logger=self.logger
        )

        # Initialisation des détecteurs
        self.detectors = Detectors(
            logger=self.logger,
            config_manager=config_manager
        )


        self.logger.info("Moteur de stratégie Scalping initialisé.")

    # ==========================================================
    # =============   API PRINCIPALE (ENTRÉE)   ================
    # ==========================================================
    # --- Dans class ScalpingStrategy(BaseStrategy): ---
    def _finalize_decision(self, decision: Dict[str, Any], analyzed_context: Dict[str, Any]) -> Dict[str, Any]:
        """
        Normalise la décision avant retour au pipeline.
        Évite que le pipeline écrase une décision valide faute de champs attendus.
        """
        if not isinstance(decision, dict):
            return {}

        # Champs standard attendus par l'executor / pipeline
        decision.setdefault("strategy_type", "scalping")
        decision.setdefault("rule_name", decision.get("rule_name", "burst_scalping"))
        decision.setdefault("execution_status", "ready")   # prêt à exécuter
        decision.setdefault("confidence", float(decision.get("confidence", 0.0) or 0.0))

        # Optionnel: petit snapshot de contexte utile au debug
        ctx = analyzed_context or {}
        decision.setdefault("context_snapshot", {
            "cycle": ctx.get("cycle_count"),
            "daily_trade_count": ctx.get("daily_trade_count"),
            "market_regime": ctx.get("market_regime"),
        })

        return decision


    def evaluate_entry(
        self,
        asset: str,
        analyzed_context: Dict[str, Any],
        asset_signals: Dict[str, Any],
    ) -> Dict[str, Any]:
        """
        Version 'desk pro' compatible pipeline:
        - Pas de fallback → uniquement des règles explicites
        - Priorité:
            1. Marubozu (MarketAnalyzer + Playbook)
            2. Range Accumulation MTF
            3. Range Accumulation simple
            4. Burst scalping
        - ATR/Spread n'affecte que le burst
        """
        try:
            # --- 0) Données & config ---
            ctx_md = (analyzed_context.get("market_data") or {}).get(asset, {}) or {}
            df_m1 = ctx_md.get("df_m1") or ctx_md.get("df")
            df_work = (
                df_m1.copy()
                if isinstance(df_m1, pd.DataFrame) and len(df_m1) >= 50
                else None
            )
            # --- 0c) Intégration footprint ---
            try:
                fp_score = float(asset_signals.get("footprint_score", 0.0))
                fp_status = str(asset_signals.get("footprint_status", "N/A")).upper()
                fp_summary = asset_signals.get("footprint_summary", {})

                # Boost confiance si footprint cohérent
                if fp_status == "BULLISH" and fp_score > 0 and asset_signals.get("phase", "").lower().startswith("bull"):
                    asset_signals["confidence_score"] = min(
                        1.0, float(asset_signals.get("confidence_score", 0.5)) + 0.15
                    )
                    self.logger.info(f"[{asset}] 📊 Footprint bullish → confiance renforcée")

                if fp_status == "BEARISH" and fp_score > 0 and asset_signals.get("phase", "").lower().startswith("bear"):
                    asset_signals["confidence_score"] = min(
                        1.0, float(asset_signals.get("confidence_score", 0.5)) + 0.15
                    )
                    self.logger.info(f"[{asset}] 📊 Footprint bearish → confiance renforcée")

                # Early entry si déséquilibre extrême
                delta = None
                try:
                    delta = fp_summary.get("delta_total")
                except Exception:
                    delta = None
                if isinstance(delta, (int, float)) and abs(delta) >= 300:  # seuil ajustable
                    asset_signals["early_entry_allowed"] = True
                    self.logger.info(f"[{asset}] ⚡ Early entry activé (Δ={delta}) via footprint")
                else:
                    asset_signals["early_entry_allowed"] = False

            except Exception as e:
                self.logger.warning(f"[{asset}] Footprint integration skipped: {e}")

            # --- 0b) Analyse via MarketAnalyzer ---
            patterns, latest_pattern = [], None
            if isinstance(df_work, pd.DataFrame):
                try:
                    market_analyzer = MarketAnalyzer(
                        config_manager=self.config_manager,
                        logger=self.logger,
                    )
                    market_results = market_analyzer.analyze(df_work, asset)

                    # On récupère les patterns détectés
                    patterns = market_results.get("patterns", {}).get("combos", [])
                    # Dernier pattern exploitable
                    latest_pattern = (
                        market_results.get("patterns", {})
                        .get("candles", [None])[-1]
                    )
                    if latest_pattern:
                        self.logger.info(
                            f"[{asset}] Dernier pattern détecté: "
                            f"{latest_pattern.get('pattern')} "
                            f"(type={latest_pattern.get('type')}, bullish={latest_pattern.get('is_bullish')})"
                        )
                        asset_signals["latest_pattern"] = latest_pattern
                except Exception as e:
                    self.logger.warning(f"[{asset}] MarketAnalyzer skipped: {e}")

            strat_cfg = (self.strategy_config or {}).copy()
            # --- 1) Métadonnées --- (déplacé plus haut pour decide_from_patterns)
            meta = self._safe_asset_meta(asset, asset_signals, analyzed_context, strat_cfg)
            pip_size = meta["pip_size"]
            if pip_size <= 0:
                self.logger.warning(f"[{asset}] pip_size invalide.")
                return {}

                    
            # --- Banque privée: décision patterns chandeliers ---
            if latest_pattern and isinstance(df_work, pd.DataFrame) and len(df_work) > 0:
                last_candle = df_work.iloc[-1]
                pattern_action = self.decide_from_patterns(
                    asset,
                    latest_pattern,
                    last_candle,
                    ctx={
                        "atr_m1_pips": None,
                        "spread_pips": meta.get("spread_pips", 999.0),
                        "trend_hint": None,  # ou ton biais SMA si dispo
                        "near_resistance": meta.get("near_resistance"),
                        "near_support": meta.get("near_support"),
                        "sma_fast_up": meta.get("sma_fast_up"),
                        "sma_fast_down": meta.get("sma_fast_down"),
                    },
                )
                if pattern_action in ("BUY", "SELL"):
                    action = pattern_action
                    self.logger.info(
                        f"[{asset}] 📊 decide_from_patterns → action={action} "
                        f"(pattern={latest_pattern.get('pattern')})"
                    )


            # Config burst_scalping
            burst_cfg = (
                strat_cfg.get("burst_scalping")
                or ((strat_cfg.get("entry_rules") or {}).get("scalping") or {}).get("burst_scalping")
                or {}
            )
           
            # --- 2) Prix courant ---
            price = self._safe_price_from_signals(asset_signals)
            if not price:
                self.logger.info(f"[{asset}] Pas de prix exploitable dans les signaux.")
                return {}

            # --- 3) Marubozu Playbook ---
            mp_cfg = (strat_cfg.get("entry_rules") or {}).get("scalping", {}).get("marubozu_playbook", {})
            if (
                mp_cfg.get("enabled", True)
                and isinstance(df_work, pd.DataFrame)
                and latest_pattern
                and "marubozu" in str(latest_pattern.get("pattern", "")).lower()
            ):
                mp_decision = self._rule_marubozu_playbook(
                    asset=asset,
                    df=df_work,
                    price=price,
                    meta=meta,
                    mtf_ctx=(analyzed_context.get("market_data") or {}).get(asset, {}),
                    cfg=mp_cfg,
                )
                if mp_decision:
                    return self._finalize_decision(mp_decision, analyzed_context)

            # --- 3b) Marubozu Impulse ---
            imp_cfg = (strat_cfg.get("entry_rules") or {}).get("scalping", {}).get("marubozu_impulse", {})
            if imp_cfg.get("enabled", True) and isinstance(df_work, pd.DataFrame):
                impulse_decision = self._rule_marubozu_impulse(
                    df=df_work, asset=asset, price=price, meta=meta, cfg=imp_cfg
                )
                if impulse_decision:
                    return self._finalize_decision(impulse_decision, analyzed_context)

            # --- 4) Biais directionnel MTF ---
            action = self._infer_action_from_signals(asset_signals)
            if action is None:
                if isinstance(df_work, pd.DataFrame) and len(df_work) >= 20:
                    sma = self._sma(df_work["close"].astype(float), 20).iloc[-1]
                    action = "BUY" if price >= sma else "SELL"
                else:
                    self.logger.info(f"[{asset}] Aucune direction claire.")
                    return {}

            # --- 5) Range Accumulation MTF ---
            try:
                mtf_cfg = strat_cfg.get("range_accumulation_mtf") or {}
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
            except Exception as e:
                self.logger.debug(f"[{asset}] MTF range-accum skipped: {e}")

            # --- 6) Range Accumulation simple ---
            try:
                range_decision = self._rule_range_accumulation(
                    df=df_work,
                    asset=asset,
                    price=price,
                    action=action,
                    meta=meta,
                    cfg=(strat_cfg.get("range_accumulation") or {}),
                )
                if range_decision:
                    range_decision.setdefault("strategy_type", "scalping")
                    range_decision.setdefault("rule_name", "range_accumulation")
                    range_decision.setdefault("execution_status", "ready")
                    return self._finalize_decision(range_decision, analyzed_context)
            except Exception as e:
                self.logger.debug(f"[{asset}] Range accumulation simple skipped: {e}")

            # --- 7) Burst scalping ---
            atr_m1_pips = None
            if isinstance(df_work, pd.DataFrame):
                atr_m1 = self._atr(df_work, period=14)
                atr_m1_pips = (
                    (atr_m1 / pip_size)
                    if isinstance(atr_m1, (int, float)) and atr_m1 > 0 and pip_size > 0
                    else None
                )

            # Config guardrails dynamique
            guardrails_cfg = {}
            try:
                guardrails_cfg = self.config_manager.get("guardrails", {}) or {}
            except Exception:
                guardrails_cfg = getattr(self.config_manager, "guardrails", {}) or {}

            # Seuils dynamiques
            try:
                min_atr_req = float(
                    burst_cfg.get(
                        "min_atr_m1_pips",
                        guardrails_cfg.get("volatility", {}).get("min_atr_m1_pips", 0.0),
                    )
                )
            except Exception:
                min_atr_req = 0.0

            try:
                max_spread_burst = float(
                    burst_cfg.get(
                        "max_spread_pips",
                        guardrails_cfg.get("volatility", {}).get("max_spread_pips", 999.0),
                    )
                )
            except Exception:
                max_spread_burst = 999.0

            ignore_all = bool(guardrails_cfg.get("ignore_all", False))
            burst_ignore_checks = bool(burst_cfg.get("ignore_checks", False))
            effective_ignore_checks = ignore_all or burst_ignore_checks

            self.logger.debug(
                f"[{asset}][SCALPING] thresholds → min_atr_m1={min_atr_req}, "
                f"max_spread={max_spread_burst}, ignore_checks={effective_ignore_checks}"
            )

            burst_allowed = True
            if not effective_ignore_checks:
                if max_spread_burst and meta.get("spread_pips", 0.0) > max_spread_burst:
                    self.logger.info(
                        f"[{asset}] REFUS BURST → spread {meta['spread_pips']:.2f}p > seuil {max_spread_burst:.2f}p"
                    )
                    burst_allowed = False

                if min_atr_req > 0.0 and (atr_m1_pips is None or atr_m1_pips < min_atr_req):
                    self.logger.info(
                        f"[{asset}] REFUS BURST → ATR M1 {atr_m1_pips or 0:.2f}p < seuil {min_atr_req:.2f}p"
                    )
                    burst_allowed = False

            if bool(burst_cfg.get("enabled", True)) and burst_allowed:
                burst_decision = self._rule_burst_scalping(
                    asset=asset,
                    action=action,
                    entry_price=price,
                    meta=meta,
                    signals={**asset_signals, "atr_m1_pips": atr_m1_pips},
                    burst_cfg=burst_cfg,
                    context=analyzed_context,
                )
                if burst_decision:
                    burst_decision.setdefault("strategy_type", "scalping")
                    burst_decision.setdefault("rule_name", "burst_scalping")
                    burst_decision.setdefault("execution_status", "ready")
                    return self._finalize_decision(burst_decision, analyzed_context)

            # --- Aucun setup valide ---
            self.logger.info(f"[DEBUG][{asset}] evaluate_entry terminé → AUCUN setup retenu.")
            return {}

        except Exception as e:
            self.logger.error(f"[{asset}] evaluate_entry error: {e}", exc_info=True)
            return {}
         
    # ==========================================================
    # =============       RÈGLES D’ENTRÉE       ================
    # ==========================================================
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
    # BANQUE PRIVÉE — Décision via Patterns Chandeliers
    # =====================================================
    @staticmethod    
    def decide_from_patterns(
        asset: str,
        latest_pattern: dict,
        last_candle,
        ctx: dict = None
    ):
        """
        Renvoie 'BUY' | 'SELL' | None selon pattern + contexte.
        Helpers _has et _candle_parts sont définis en local pour éviter les NameError.
        """

        # --- helpers locaux (aucune dépendance externe) ---
        def _has(pname: str, *keys) -> bool:
            pname = (pname or "").lower()
            return any(k.lower() in pname for k in keys)

        def _candle_parts(c):
            # support objet OHLC (attributs) ou pandas Series (clés)
            def _get(obj, key):
                if hasattr(obj, key):
                    return getattr(obj, key)
                if isinstance(obj, dict) and key in obj:
                    return obj[key]
                # pandas Series
                try:
                    return obj[key]
                except Exception:
                    return None

            o = float(_get(c, "open"))
            h = float(_get(c, "high"))
            l = float(_get(c, "low"))
            cl = float(_get(c, "close"))

            rng = max(1e-9, h - l)
            body = abs(cl - o)
            is_green = cl >= o
            upper_wick = h - max(o, cl)
            lower_wick = min(o, cl) - l
            return o, h, l, cl, body, rng, upper_wick, lower_wick, is_green

        # --- extraction candle + pattern ---
        pname = str((latest_pattern or {}).get("pattern", "")).lower()
        o, h, l, cl, body, rng, wu, wd, is_green = _candle_parts(last_candle)

        # --- contexte/guardrails ---
        ctx = ctx or {}
        atr_pips      = ctx.get("atr_m1_pips")
        min_atr_pips  = ctx.get("min_atr_pips", 0.1)
        spread_pips   = ctx.get("spread_pips", 999.0)
        max_spread    = ctx.get("max_spread_pips", 999.0)
        trend_hint    = ctx.get("trend_hint")                # 'up' | 'down' | None
        near_res      = bool(ctx.get("near_resistance", False))
        near_sup      = bool(ctx.get("near_support", False))
        sma_fast_up   = bool(ctx.get("sma_fast_up", False))
        sma_fast_down = bool(ctx.get("sma_fast_down", False))

        # Liquidity / ATR / spread
        if atr_pips is not None and atr_pips < float(min_atr_pips):
            return None
        if float(spread_pips) > float(max_spread):
            return None

        # --- règles unitaires ---
        action = None

        # Engulfing
        if _has(pname, "bullish engulfing", "engulfing_bull") and is_green:
            action = "BUY"
        if _has(pname, "bearish engulfing", "engulfing_bear") and not is_green:
            action = "SELL"

        # Marubozu
        if _has(pname, "marubozu"):
            action = "BUY" if is_green else "SELL"

        # Doji/indécision → reject
        if _has(pname, "doji", "spinning_top") or (body < 0.1 * rng):
            return None

        # Hammer / Hanging man
        hammer_like = (wd > 2 * body and wu < body)
        if _has(pname, "hammer") or hammer_like:
            if is_green and not near_res:
                action = "BUY"
            else:
                action = None

        if _has(pname, "hanging man"):
            if not is_green and not near_sup:
                action = "SELL"

        # Shooting star
        shoot_like = (wu > 2 * body and wd < body)
        if _has(pname, "shooting_star") or shoot_like:
            if not is_green and not near_sup:
                action = "SELL"

        # --- combos ---
        if _has(pname, "morning_star"): action = "BUY"
        if _has(pname, "evening_star"): action = "SELL"
        if _has(pname, "harami_bull") and is_green: action = "BUY"
        if _has(pname, "harami_bear") and not is_green: action = "SELL"
        if _has(pname, "tweezer_bottom"): action = "BUY"
        if _has(pname, "tweezer_top"):    action = "SELL"
        if _has(pname, "three_white_soldiers"): action = "BUY"
        if _has(pname, "three_black_crows"):    action = "SELL"

        # --- figures chartistes ---
        if _has(pname, "double_bottom"): action = "BUY"
        if _has(pname, "double_top"):    action = "SELL"
        if _has(pname, "inverse_head_shoulders", "inv_head_shoulders"): action = "BUY"
        if _has(pname, "head_shoulders", "head-and-shoulders"):          action = "SELL"
        if _has(pname, "bull_flag", "bull_pennant"):  action = "BUY"
        if _has(pname, "bear_flag", "bear_pennant"):  action = "SELL"
        if _has(pname, "ascending_triangle"):  action = "BUY"
        if _has(pname, "descending_triangle"): action = "SELL"
        if _has(pname, "symmetrical_triangle"): action = None  # neutre

        # --- confluences directionnelles ---
        if action == "BUY"  and trend_hint == "down": action = None
        if action == "SELL" and trend_hint == "up":   action = None
        if action == "BUY"  and sma_fast_down:        action = None
        if action == "SELL" and sma_fast_up:          action = None
        if action == "BUY"  and near_res:             action = None
        if action == "SELL" and near_sup:             action = None

        # --- filtres finaux de cohérence (no BUY sur bougie rouge, etc.) ---
        if action == "BUY"  and not is_green: return None
        if action == "SELL" and is_green:      return None

        return action

    # =====================================================
    # Tes règles scalping/liquidity existantes commencent ici
    # =====================================================

    def _rule_burst_scalping(
        self,
        asset: str,
        action: str,
        entry_price: float,
        meta: Dict[str, Any],
        signals: Dict[str, Any],
        burst_cfg: Dict[str, Any],
        context: Dict[str, Any],
    ) -> Optional[Dict[str, Any]]:
        """
        Burst Scalping (risk-based):
        - Ouvre un panier de N ordres d’un coup
        - Volume calculé dynamiquement selon risk_per_trade_percent
        - SL obligatoire, pas de TP (gestion via trailing stop)
        - Respecte strictement burst_size et max_bursts de la config
        - Refuse tout nouveau burst tant qu’un panier burst pour l’actif est encore ouvert
        """
        import math, uuid, re

        # === Lecture config ===
        size = int(burst_cfg.get("burst_size", 3))          # nombre d’ordres par burst
        max_bursts = int(burst_cfg.get("max_bursts", 1))    # nombre de bursts autorisés en parallèle
        if size <= 0:
            return None

        # --- Infos broker ---
        symbol_info = context.get("symbol_info", {}) or {}
        point = float(symbol_info.get("point", 0.01))
        pip_size_value = point * 10.0  # ex: 1 pip = 10 points

        contract_size = float(symbol_info.get("trade_contract_size", 100000))
        tick_value = float(symbol_info.get("trade_tick_value", 1.0))
        tick_size = float(symbol_info.get("trade_tick_size", 0.0001))
        value_per_point = tick_value / tick_size if tick_size > 0 else 1.0

        # --- Config risk management ---
        account_info = context.get("account_info", {}) or {}
        equity = float(account_info.get("equity", 0.0) or 0.0)
        risk_pct = float(burst_cfg.get("risk_per_trade_percent", 3.0))

        # Répartir le risque sur l’ensemble du panier
        max_risk = (equity * (risk_pct / 100.0)) / max(1, size)

        # --- SL en pips depuis config ---
        sl_pips = burst_cfg.get("sl_pips", 5.0)
        if not isinstance(sl_pips, (int, float)) or sl_pips <= 0:
            sl_pips = 5.0

        # --- Distance SL en prix ---
        if action.upper() == "BUY":
            sl_price = entry_price - sl_pips * pip_size_value
            sl_distance_price = entry_price - sl_price
        else:
            sl_price = entry_price + sl_pips * pip_size_value
            sl_distance_price = sl_price - entry_price
        sl_distance_price = abs(sl_distance_price)

        # --- Risque par lot ---
        risk_per_lot = sl_distance_price * value_per_point
        volume = (max_risk / risk_per_lot) if risk_per_lot > 0 else 0.0

        # --- Normalisation broker ---
        min_lot = float(symbol_info.get("volume_min", 0.01))
        lot_step = float(symbol_info.get("volume_step", 0.01))
        max_lot = float(symbol_info.get("volume_max", 100.0))

        if lot_step > 0:
            volume = math.floor(volume / lot_step) * lot_step
        volume = max(min_lot, min(max_lot, volume))

        if volume <= 0:
            self.logger.error(f"[{asset}] ❌ Volume calculé invalide ({volume}).")
            return None

        # --- Détection des paniers actifs via commentaire MT5 ---
        open_positions = getattr(self.mt5_connector, "get_open_positions", lambda: [])()
        basket_pat = re.compile(r"burst_scalping\|basket=([A-Za-z0-9_]+)", re.IGNORECASE)

        active_baskets_for_asset = set()
        for pos in open_positions or []:
            pos_sym = pos.get("symbol") or pos.get("asset")
            if pos_sym and str(pos_sym).upper() == asset.upper():
                comment = str(pos.get("comment", "")) or ""
                m = basket_pat.search(comment)
                if m:
                    active_baskets_for_asset.add(m.group(1))

        # Refus strict si un burst existe déjà pour cet actif (et max_bursts=1)
        if len(active_baskets_for_asset) >= max_bursts:
            self.logger.warning(
                f"[{asset}] Refus nouveau burst: {len(active_baskets_for_asset)}/{max_bursts} panier(s) déjà actif(s) pour {asset}."
            )
            return None

        # --- Construire le panier ---
        basket_id = f"burst_{asset}_{uuid.uuid4().hex[:8]}"
        decisions: List[Dict[str, Any]] = []

        for i in range(size):
            comment = f"burst_scalping|basket={basket_id}|{i+1}/{size}"
            d: Dict[str, Any] = {
                "action": action,
                "asset": asset,
                "order_type": "MARKET",
                "entry_price": entry_price,
                "sl_price": sl_price,
                "tp_price": None,  # pas de TP (trailing)
                "volume": volume,
                "rule_name": "burst_scalping",
                "strategy_type": "scalping",
                "basket_id": basket_id,
                "burst_index": i + 1,
                "burst_size": size,
                "meta": {"burst": True, "entry_source": "core_decision"},
                "comment": comment,  # clé : on sérialise le basket dans le comment
            }
            decisions.append(d)

        self.logger.info(
            f"[{asset}] 🔥 Burst Scalping: {size}x {action} @ {entry_price} | "
            f"SL={sl_price} | volume={volume:.2f} | risk={risk_pct}% | basket_id={basket_id}"
        )
        return {"burst_decisions": decisions, "basket_id": basket_id}


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
        BUY si on casse le plus bas récent (sweep bas), SELL si on casse le plus haut récent (sweep haut).
        """
        if df is None or len(df) < lookback:
            return None

        recent = df.tail(lookback)
        hh = float(recent["high"].max())
        ll = float(recent["low"].min())
        last = df.iloc[-1]
        close = float(last["close"])

        # heuristique sweep : close au-delà de HH/LL
        if close >= hh:
            return "SELL"  # prise de liquidité au-dessus → contrarian
        if close <= ll:
            return "BUY"
        return None

    def _rule_marubozu_playbook(
        self,
        asset: str,
        df: pd.DataFrame,
        price: float,
        meta: Dict[str, Any],
        mtf_ctx: Dict[str, Any],
        cfg: Dict[str, Any],
    ) -> Optional[Dict[str, Any]]:
        """
        Détecte marubozu / réintégration / range actif et propose un trade.
        Patchée pour intégrer le dernier pattern bougie détecté.
        """

        # --- A) Biais MTF ---
        if cfg.get("mtf_bias", {}).get("use", True):
            bias_h1 = (mtf_ctx.get("bias_h1") or "").lower()
            bias_m15 = (mtf_ctx.get("bias_m15") or "").lower()
            prefer_h1 = cfg["mtf_bias"].get("prefer_h1", True)
            block_against_both = cfg["mtf_bias"].get("block_against_both", True)

            def dir_ok(direction: str) -> bool:
                def bias_to_dir(b):
                    return (
                        "buy"
                        if "up" in b or "bull" in b
                        else ("sell" if "down" in b or "bear" in b else "neutre")
                    )

                d_h1, d_m15 = bias_to_dir(bias_h1), bias_to_dir(bias_m15)
                if block_against_both and d_h1 != "neutre" and d_m15 != "neutre":
                    if direction == "buy" and d_h1 == "sell" and d_m15 == "sell":
                        return False
                    if direction == "sell" and d_h1 == "buy" and d_m15 == "buy":
                        return False
                if prefer_h1 and d_h1 != "neutre" and direction != d_h1:
                    pass
                return True

        # --- B) Helpers ---
        def last_big_candle(df_, atr_period, min_mult, min_body):
            if len(df_) < atr_period + 3:
                return None
            atr = self._atr(df_, period=atr_period)
            if not isinstance(atr, (int, float)) or atr <= 0:
                return None
            body = (df_["close"] - df_["open"]).abs()
            size = (df_["high"] - df_["low"]).abs()
            body_ratio = (body / size.replace(0, np.nan)).fillna(0.0)
            for idx in range(len(df_) - 1, max(len(df_) - 4, 1), -1):
                csize, brat = float(size.iloc[idx]), float(body_ratio.iloc[idx])
                if csize >= min_mult * atr and brat >= min_body:
                    return {
                        "index": idx,
                        "bull": df_["close"].iloc[idx] > df_["open"].iloc[idx],
                        "size": csize,
                        "body_ratio": brat,
                        "high": float(df_["high"].iloc[idx]),
                        "low": float(df_["low"].iloc[idx]),
                        "open": float(df_["open"].iloc[idx]),
                        "close": float(df_["close"].iloc[idx]),
                        "mid": float(
                            (df_["open"].iloc[idx] + df_["close"].iloc[idx]) / 2.0
                        ),
                    }
            return None

        def is_range_active(df_, lookback, max_range_over_atr, min_avg_candle_atr):
            if len(df_) < lookback + 10:
                return False, None
            sub = df_.tail(lookback)
            rng = float(sub["high"].max() - sub["low"].min())
            atr = self._atr(df_, period=14)
            if not isinstance(atr, (int, float)) or atr <= 0:
                return False, None
            avg_candle = float((sub["high"] - sub["low"]).mean())
            if (
                rng / atr <= max_range_over_atr
                and (avg_candle / atr) >= min_avg_candle_atr
            ):
                return True, {
                    "hh": float(sub["high"].max()),
                    "ll": float(sub["low"].min()),
                }
            return False, None

        # --- C) Détection ---
        det = cfg.get("detection", {})
        big = last_big_candle(
            df,
            atr_period=int(det.get("atr_period", 14)),
            min_mult=float(det.get("min_atr_mult", 2.2)),
            min_body=float(det.get("min_body_ratio", 0.85)),
        )
        latest_pat = meta.get("latest_pattern")

        if big:
            direction = "buy" if big["bull"] else "sell"

            # (1) Continuation
            if cfg.get("continuation", {}).get("enabled", True):
                pmin = float(cfg["continuation"].get("pullback_frac_min", 0.2))
                pmax = float(cfg["continuation"].get("pullback_frac_max", 0.4))
                hi, lo = big["high"], big["low"]
                body_top = max(big["open"], big["close"])
                body_bot = min(big["open"], big["close"])
                pull_min = (
                    body_top - pmax * (body_top - body_bot)
                    if big["bull"]
                    else body_bot + pmax * (body_top - body_bot)
                )
                pull_max = (
                    body_top - pmin * (body_top - body_bot)
                    if big["bull"]
                    else body_bot + pmin * (body_top - body_bot)
                )
                in_zone = (
                    (pull_min <= price <= pull_max)
                    if big["bull"]
                    else (pull_max <= price <= pull_min)
                )

                if in_zone and (
                    not cfg.get("mtf_bias", {}).get("use", True) or dir_ok(direction)
                ):
                    sl = (
                        (lo - meta["pip_size"] * 2)
                        if big["bull"]
                        else (hi + meta["pip_size"] * 2)
                    )
                    sl_pips = abs(price - sl) / meta["pip_size"]
                    tp_pips = sl_pips * float(cfg["continuation"].get("rr_target", 1.5))
                    dec = {
                        "action": "BUY" if big["bull"] else "SELL",
                        "asset": asset,
                        "entry_price": price,
                        "target_sl_pips": round(sl_pips, 2),
                        "target_tp_pips": round(tp_pips, 2),
                        "rule_name": "marubozu_continuation",
                        "strategy_type": "scalping",
                        "confidence": 0.7,
                        "meta": {"marubozu": big},
                    }
                    if latest_pat:
                        dec["rule_name"] += f"+pattern:{latest_pat.get('pattern')}"
                        dec["meta"]["pattern"] = latest_pat
                    return dec

            # (2) Reversal
            if (
                cfg.get("reversal", {}).get("enabled", True)
                and len(df) > big["index"] + 1
            ):
                nxt = big["index"] + 1
                mid = big["mid"]
                next_close, next_open = float(df["close"].iloc[nxt]), float(
                    df["open"].iloc[nxt]
                )
                reintegrated = (next_close < mid) if big["bull"] else (next_close > mid)
                closed_opposite = (
                    (next_close < next_open)
                    if big["bull"]
                    else (next_close > next_open)
                )

                if reintegrated and closed_opposite:
                    sl = (
                        (big["high"] + meta["pip_size"] * 2)
                        if big["bull"]
                        else (big["low"] - meta["pip_size"] * 2)
                    )
                    sl_pips = abs(price - sl) / meta["pip_size"]
                    tp_pips = sl_pips * float(cfg["reversal"].get("rr_target", 1.2))
                    dec = {
                        "action": "SELL" if big["bull"] else "BUY",
                        "asset": asset,
                        "entry_price": price,
                        "target_sl_pips": round(sl_pips, 2),
                        "target_tp_pips": round(tp_pips, 2),
                        "rule_name": "marubozu_reversal",
                        "strategy_type": "scalping",
                        "confidence": 0.65,
                        "meta": {"marubozu": big},
                    }
                    if latest_pat:
                        dec["rule_name"] += f"+pattern:{latest_pat.get('pattern')}"
                        dec["meta"]["pattern"] = latest_pat
                    return dec

        # --- D) Range actif ---
        rg_cfg = cfg.get("range_active", {})
        if rg_cfg.get("enabled", True):
            ok, info = is_range_active(
                df,
                lookback=int(rg_cfg.get("lookback_bars", 20)),
                max_range_over_atr=float(rg_cfg.get("max_range_over_atr", 3.0)),
                min_avg_candle_atr=float(rg_cfg.get("min_avg_candle_atr", 0.9)),
            )
            if ok and info:
                hh, ll = info["hh"], info["ll"]
                tol = float(rg_cfg.get("tolerance_frac", 0.15))
                top_zone, bot_zone = hh - tol * (hh - ll), ll + tol * (hh - ll)
                dec = None
                if price >= top_zone:
                    dec = {
                        "action": "SELL",
                        "asset": asset,
                        "entry_price": price,
                        "target_sl_pips": float(rg_cfg.get("sl_pips", 30)),
                        "target_tp_pips": 0.0,
                        "rule_name": "range_active_top",
                        "strategy_type": "scalping",
                        "confidence": 0.65,
                        "meta": {"range_high": hh, "range_low": ll},
                    }
                elif price <= bot_zone:
                    dec = {
                        "action": "BUY",
                        "asset": asset,
                        "entry_price": price,
                        "target_sl_pips": float(rg_cfg.get("sl_pips", 30)),
                        "target_tp_pips": 0.0,
                        "rule_name": "range_active_low",
                        "strategy_type": "scalping",
                        "confidence": 0.65,
                        "meta": {"range_high": hh, "range_low": ll},
                    }
                if dec and latest_pat:
                    dec["rule_name"] += f"+pattern:{latest_pat.get('pattern')}"
                    dec["meta"]["pattern"] = latest_pat
                return dec

        return None

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

        top_zone, bot_zone = hh - tolerance * rng, ll + tolerance * rng
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
            pat_name = str(latest_pat.get("pattern", "")).lower()
            pat_bull = latest_pat.get("is_bullish")
            if dec["action"] == "BUY" and pat_bull is True:
                dec["confidence"] += 0.2
            elif dec["action"] == "SELL" and pat_bull is False:
                dec["confidence"] += 0.2
            elif pat_bull is not None:
                dec["confidence"] -= 0.1
            dec["rule_name"] += f"+pattern:{pat_name}"
            dec["meta"] = dec.get("meta", {})
            dec["meta"]["pattern"] = latest_pat

        return dec

    # === Règle Marubozu / Impulsion ===
    def _rule_marubozu_impulse(
        self,
        df: pd.DataFrame,
        asset: str,
        price: float,
        meta: Dict[str, Any],
        cfg: Dict[str, Any],
    ) -> Optional[Dict[str, Any]]:
        """
        Détecte une bougie marubozu (impulsion propre, sans mèches)
        et propose un trade dans sa direction.
        - Utilise pip_size pour calculer SL/TP
        - Intègre le dernier pattern bougie détecté (latest_pattern)
        - Jamais bloquant : retourne None si les conditions ne sont pas réunies
        """
        if len(df) < 2:
            return None

        last = df.iloc[-1]
        size = float(last["high"] - last["low"])
        body = abs(float(last["close"] - last["open"]))
        upper_wick = float(last["high"] - max(last["open"], last["close"]))
        lower_wick = float(min(last["open"], last["close"]) - last["low"])

        pip_size = meta.get("pip_size", 0.0001)
        latest_pat = meta.get("latest_pattern")

        # --- Critères marubozu ---
        if size <= 0:
            return None

        body_ratio = body / size
        min_body_ratio = float(cfg.get("min_body_ratio", 0.9))  # ex: 90% du range
        max_wick_ratio = float(cfg.get("max_wick_ratio", 0.1))  # ex: mèches < 10% du corps

        if body_ratio >= min_body_ratio and upper_wick <= max_wick_ratio * body and lower_wick <= max_wick_ratio * body:
            action = "BUY" if last["close"] > last["open"] else "SELL"

            # SL : derrière l'extrême
            sl_price = (last["low"] - 2 * pip_size) if action == "BUY" else (last["high"] + 2 * pip_size)
            sl_pips = abs(price - sl_price) / pip_size

            # TP : R:R basé sur config
            rr_target = float(cfg.get("rr_target", 2.0))
            tp_pips = sl_pips * rr_target
            tp_price = price + tp_pips * pip_size if action == "BUY" else price - tp_pips * pip_size

            decision = {
                "action": action,
                "asset": asset,
                "entry_price": price,
                "sl_price": round(sl_price, 5),
                "tp_price": round(tp_price, 5),
                "target_sl_pips": round(sl_pips, 2),
                "target_tp_pips": round(tp_pips, 2),
                "rule_name": "marubozu_impulse",
                "strategy_type": "scalping",
                "confidence": 0.8,
                "meta": {
                    "body_ratio": round(body_ratio, 3),
                    "upper_wick": round(upper_wick, 5),
                    "lower_wick": round(lower_wick, 5),
                },
            }

            # Ajout éventuel du dernier pattern bougie
            if latest_pat:
                decision["rule_name"] += f"+pattern:{latest_pat.get('pattern')}"
                decision["meta"]["pattern"] = latest_pat

            return decision

        return None
    
         # --- API attendue par BaseStrategy (stubs fonctionnels) ---
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

    def evaluate_exit(self, context: Dict[str, Any], open_positions: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
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
        if df is None or len(df) < period + 2:
            return float("nan")
        h = df["high"].astype(float)
        l = df["low"].astype(float)
        c = df["close"].astype(float)
        pc = c.shift(1)
        tr = np.maximum.reduce([(h - l).abs(), (h - pc).abs(), (l - pc).abs()])
        atr = tr.rolling(window=period, min_periods=period).mean().iloc[-1]
        return float(atr) if pd.notna(atr) and atr > 0 else float("nan")

    # --- Price Action light (au cas où tu veux filtrer)
    @staticmethod
    def _is_engulfing(
        o: float, h: float, l: float, c: float, oo: float, cc: float
    ) -> bool:
        # engulfing sur 2 bougies (précédente: oo->cc, actuelle: o->c)
        body_prev = abs(cc - oo)
        body_now = abs(c - o)
        if body_prev <= 0 or body_now <= 0:
            return False
        # avale complètement
        bull = (cc > oo) and (c < o) and (o > cc) and (c < oo)
        bear = (cc < oo) and (c > o) and (o < cc) and (c > oo)
        return bull or bear

    @staticmethod
    def _is_pinbar(o: float, h: float, l: float, c: float) -> bool:
        rng = h - l
        body = abs(c - o)
        if rng <= 0:
            return False
        upper = h - max(o, c)
        lower = min(o, c) - l
        return (upper >= 2 * body and lower <= body) or (
            lower >= 2 * body and upper <= body
        )

    @staticmethod
    def _is_doji(o: float, c: float, h: float, l: float) -> bool:
        rng = h - l
        body = abs(c - o)
        return rng > 0 and (body / rng) <= 0.1
