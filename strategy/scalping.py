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

    def __init__(self, logger, config_manager):
        self.logger = logger
        self.config_manager = config_manager
        self.detectors = Detectors(logger=logger, config_manager=config_manager)

    # ==========================================================
    # =============   API PRINCIPALE (ENTRÉE)   ================
    # ==========================================================
    # --- Dans class ScalpingStrategy(BaseStrategy): ---

    def evaluate_entry(
        self,
        asset: str,
        analyzed_context: Dict[str, Any],
        asset_signals: Dict[str, Any],
    ) -> Dict[str, Any]:
        """
        Version 'desk pro' compatible pipeline:
        - Pas de fallback → uniquement des règles explicites
        - Priorité: Marubozu > Range Accumulation MTF > Range Accumulation simple > Burst scalping
        - ATR/Spread n'affecte que le burst, jamais les règles indépendantes
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

            strat_cfg = (
                self.config_manager.get_strategy_config("scalping") or {}
            ).copy()

            # Compatibilité burst_scalping
            burst_cfg = (
                strat_cfg.get("burst_scalping")
                or ((strat_cfg.get("entry_rules") or {}).get("scalping") or {}).get(
                    "burst_scalping"
                )
                or {}
            )

            # --- 1) Métadonnées ---
            meta = self._safe_asset_meta(
                asset, asset_signals, analyzed_context, strat_cfg
            )
            pip_size = meta["pip_size"]
            if pip_size <= 0:
                self.logger.warning(f"[{asset}] pip_size invalide.")
                return {}

            # --- 2) Prix courant ---
            price = self._safe_price_from_signals(asset_signals)
            if not price:
                self.logger.info(f"[{asset}] Pas de prix exploitable dans les signaux.")
                return {}

            # --- 3) Marubozu Playbook ---
            mp_cfg = (
                (strat_cfg.get("entry_rules") or {})
                .get("scalping", {})
                .get("marubozu_playbook", {})
            )
            if mp_cfg.get("enabled", True) and isinstance(df_work, pd.DataFrame):
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

            # --- 6) Range Accumulation simple (indépendante) ---
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
                    return range_decision
            except Exception as e:
                self.logger.debug(f"[{asset}] Range accumulation simple skipped: {e}")

            # --- 7) Burst scalping (protégé par ATR/Spread) ---
            atr_m1_pips = None
            if isinstance(df_work, pd.DataFrame):
                atr_m1 = self._atr(df_work, period=14)
                atr_m1_pips = (
                    (atr_m1 / pip_size)
                    if isinstance(atr_m1, (int, float)) and atr_m1 > 0 and pip_size > 0
                    else None
                )

            min_atr_req = float(burst_cfg.get("min_atr_m1_pips", 0.0))
            max_spread_burst = float(burst_cfg.get("max_spread_pips", 999))

            burst_allowed = True
            if meta["spread_pips"] > max_spread_burst:
                self.logger.info(
                    f"[{asset}] Burst refusé: spread {meta['spread_pips']:.2f}p > {max_spread_burst:.2f}p."
                )
                burst_allowed = False
            if min_atr_req > 0 and (atr_m1_pips is None or atr_m1_pips < min_atr_req):
                self.logger.info(
                    f"[{asset}] Burst refusé: ATR M1 {atr_m1_pips or 0:.1f}p < {min_atr_req:.1f}p."
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
                    return burst_decision

            # --- Aucun setup valide ---
            return {}

        except Exception as e:
            self.logger.error(f"[{asset}] evaluate_entry error: {e}", exc_info=True)
            return {}

    # ==========================================================
    # =============       RÈGLES D’ENTRÉE       ================
    # ==========================================================
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
        Ouvre un panier (burst) de N ordres d’un coup.
        Garde-fous :
            - ATR M1 min (soft)
            - Spread max
            - Confirmation directionnelle M1 (optionnelle)
        Niveaux :
            - SL/TP en pips si fournis, sinon laissés au TradeExecutor
        """
        size = int(burst_cfg.get("size", 5))
        if size <= 0:
            return None

        # Garde ATR/Spread
        min_atr_m1 = float(burst_cfg.get("min_atr_m1_pips", 0.0))

        atr_value_pips: Optional[float] = None
        for candidate in (
            signals.get("atr_m1_pips"),
            context.get("atr_m1_pips"),
        ):
            if candidate is None:
                continue
            try:
                atr_value_pips = float(candidate)
                break
            except (TypeError, ValueError):
                continue

        if atr_value_pips is None:
            raw_atr = signals.get("atr_m1")
            pip_size = float(meta.get("pip_size", 0.0) or 0.0)
            if raw_atr is not None and pip_size > 0:
                try:
                    atr_value_pips = float(raw_atr) / pip_size
                except (TypeError, ValueError):
                    atr_value_pips = None

        if atr_value_pips is None:
            atr_value_pips = 0.0

        if min_atr_m1 > 0 and atr_value_pips < min_atr_m1:
            self.logger.info(
                f"[{asset}] Burst refusé: ATR M1 {atr_value_pips:.2f}p < min {min_atr_m1}p."
            )
            return None

        max_spread_burst = float(burst_cfg.get("max_spread_pips", 999))
        spread_pips = float(meta.get("spread_pips", 0.0) or 0.0)
        if spread_pips > max_spread_burst:
            self.logger.info(
                f"[{asset}] Burst refusé: spread {spread_pips:.2f}p > {max_spread_burst}p."
            )
            return None

        # Confirmation directionnelle M1 (facultative)
        if burst_cfg.get("require_m1_bias", False):
            m1_bias = str(signals.get("m1_bias", "")).lower()
            if (action == "BUY" and m1_bias != "up") or (
                action == "SELL" and m1_bias != "down"
            ):
                self.logger.info(
                    f"[{asset}] Burst refusé: m1_bias={m1_bias} incompatible avec action={action}."
                )
                return None

        # Niveaux pips (optionnels)
        sl_pips = burst_cfg.get("sl_pips")
        tp_pips = burst_cfg.get("tp_pips")

        # Construire le panier
        basket_id = f"burst_{asset}_{uuid.uuid4().hex[:8]}"
        decisions: List[Dict[str, Any]] = []
        for i in range(size):
            d: Dict[str, Any] = {
                "action": action,
                "asset": asset,
                "order_type": "MARKET",
                "entry_price": entry_price,
                "rule_name": "burst_scalping",
                "strategy_type": "scalping",  # ✅ ajouté pour cohérence
                "basket_id": basket_id,
                "burst_index": i + 1,
                "burst_size": size,
                "meta": {"burst": True},
            }

            if isinstance(sl_pips, (int, float)) and sl_pips > 0:
                d["target_sl_pips"] = float(sl_pips)
            if isinstance(tp_pips, (int, float)) and tp_pips > 0:
                d["target_tp_pips"] = float(tp_pips)
            decisions.append(d)

        self.logger.info(
            f"[{asset}] 🔥 Burst Scalping: {size}x {action} @ {entry_price} | basket_id={basket_id}"
        )
        return {"burst_decisions": decisions, "basket_id": basket_id}

    # --- Helpers MTF pour la règle range/accumulation ---

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
        - On regarde un lookback court (ex: 20 bougies M1)
        - Si le prix tape proche du max → SELL
        - Si le prix tape proche du min → BUY
        """
        lookback = int(cfg.get("lookback_bars", 20))
        tolerance = float(cfg.get("tolerance_frac", 0.15))  # ex: 15% du range

        if len(df) < lookback:
            return None

        recent = df.tail(lookback)
        hh = float(recent["high"].max())
        ll = float(recent["low"].min())
        rng = hh - ll
        if rng <= 0:
            return None

        # zones haut/bas avec tolérance
        top_zone = hh - tolerance * rng
        bot_zone = ll + tolerance * rng

        decision = None
        if price >= top_zone:
            decision = {
                "action": "SELL",
                "asset": asset,
                "entry_price": price,
                "target_sl_pips": float(cfg.get("sl_pips", 30)),
                "target_tp_pips": float(cfg.get("tp_pips", 30)),
                "rule_name": "range_accumulation_top",
                "strategy_type": "scalping",
                "confidence": 0.7,
                "meta": {"range_high": hh, "range_low": ll},
            }
        elif price <= bot_zone:
            decision = {
                "action": "BUY",
                "asset": asset,
                "entry_price": price,
                "target_sl_pips": float(cfg.get("sl_pips", 30)),
                "target_tp_pips": float(cfg.get("tp_pips", 30)),
                "rule_name": "range_accumulation_low",
                "strategy_type": "scalping",
                "confidence": 0.7,
                "meta": {"range_high": hh, "range_low": ll},
            }

        if decision:
            self.logger.info(
                f"[{asset}] 🎯 Range Accumulation détectée: {decision['action']} @ {price}"
            )

            # === BONUS : détection d’impulsion avant range (non bloquant) ===
            impulse_detected = False
            if len(df) > lookback * 2:
                prev_segment = df.tail(lookback * 2).head(lookback)
                rng_prev = prev_segment["high"].max() - prev_segment["low"].min()
                rng_recent = rng
                if rng_prev > 2 * rng_recent:  # impulsion suivie d’un range
                    impulse_detected = True

            if impulse_detected:
                decision["confidence"] = round(
                    min(1.0, decision["confidence"] + 0.15), 3
                )
                decision["meta"]["impulse_context"] = "detected"
            else:
                decision["meta"]["impulse_context"] = "absent"

            return decision

        return None

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
        Jamais bloquant : retourne None si pas de setup propre.
        """

        # --- A) Biais MTF (facultatif mais recommandé) ---
        if cfg.get("mtf_bias", {}).get("use", True):
            bias_h1 = (mtf_ctx.get("bias_h1") or "").lower()
            bias_m15 = (mtf_ctx.get("bias_m15") or "").lower()
            prefer_h1 = cfg["mtf_bias"].get("prefer_h1", True)
            block_against_both = cfg["mtf_bias"].get("block_against_both", True)

            def dir_ok(direction: str) -> bool:
                # direction ∈ {"buy","sell"}
                if not direction:
                    return True

                # Map bias text → dir
                def bias_to_dir(b):
                    return (
                        "buy"
                        if "up" in b or "bull" in b
                        else ("sell" if "down" in b or "bear" in b else "neutre")
                    )

                d_h1 = bias_to_dir(bias_h1)
                d_m15 = bias_to_dir(bias_m15)

                if block_against_both and d_h1 != "neutre" and d_m15 != "neutre":
                    if direction == "buy" and (d_h1 == "sell" and d_m15 == "sell"):
                        return False
                    if direction == "sell" and (d_h1 == "buy" and d_m15 == "buy"):
                        return False

                if prefer_h1 and d_h1 != "neutre" and direction != d_h1:
                    # On n’interdit pas, mais on pénalise plus bas si besoin
                    pass
                return True

        # --- B) Utilitaires locaux ---
        def last_big_candle(df_, atr_period, min_mult, min_body):
            if len(df_) < atr_period + 3:
                return None
            atr = self._atr(df_, period=atr_period)
            if not isinstance(atr, (int, float)) or atr <= 0:
                return None
            body = (df_["close"] - df_["open"]).abs()
            size = (df_["high"] - df_["low"]).abs()
            body_ratio = (body / size.replace(0, np.nan)).fillna(0.0)

            for idx in range(
                len(df_) - 1,
                max(len(df_) - 1 - int(cfg["detection"].get("lookback_bars", 3)), 1),
                -1,
            ):
                csize = float(size.iloc[idx])
                brat = float(body_ratio.iloc[idx])
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

        # --- C) Détection marubozu / continuation / inversion ---
        det = cfg.get("detection", {})
        big = last_big_candle(
            df,
            atr_period=int(det.get("atr_period", 14)),
            min_mult=float(det.get("min_atr_mult", 2.2)),
            min_body=float(det.get("min_body_ratio", 0.85)),
        )

        if big:
            direction = "buy" if big["bull"] else "sell"

            # (1) Continuation : pullback dans [min,max] du corps
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
                    if cfg["continuation"].get("use_trailing", False):
                        dec["trailing"] = cfg["continuation"]["trailing"]
                    return dec

            # (2) Inversion : réintégration >= 50% + close opposée
            if (
                cfg.get("reversal", {}).get("enabled", True)
                and len(df) > big["index"] + 1
            ):
                nxt = big["index"] + 1
                mid = big["mid"]
                next_close = float(df["close"].iloc[nxt])
                next_open = float(df["open"].iloc[nxt])
                reintegrated = (next_close < mid) if big["bull"] else (next_close > mid)
                closed_opposite = (
                    (next_close < next_open)
                    if big["bull"]
                    else (next_close > next_open)
                )

                if reintegrated and closed_opposite:
                    # entrer contre
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
                    if cfg["reversal"].get("use_trailing", True):
                        dec["trailing"] = cfg["reversal"]["trailing"]
                    return dec

        # --- D) Range actif (grosses bougies qui oscillent, pas d’impulsion unique) ---
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
                top_zone = hh - tol * (hh - ll)
                bot_zone = ll + tol * (hh - ll)

                if price >= top_zone:
                    dec = {
                        "action": "SELL",
                        "asset": asset,
                        "entry_price": price,
                        "target_sl_pips": float(rg_cfg.get("sl_pips", 30)),
                        "target_tp_pips": 0.0,  # géré par tp_mode/trailing
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
                else:
                    dec = None

                if dec:
                    # trailing optionnel
                    if rg_cfg.get("use_trailing", True):
                        dec["trailing"] = rg_cfg["trailing"]
                    # TP logique (milieu/opposé) géré côté exécution si tu veux
                    dec["meta"]["tp_mode"] = rg_cfg.get("tp_mode", "mid_or_opposite")
                    return dec

                # === Règle Range Accumulation ===

    def _rule_range_accumulation(
        self, df: pd.DataFrame, asset: str, price: float, cfg: Dict[str, Any]
    ) -> Optional[Dict[str, Any]]:
        """
        Détecte un range plat (accumulation) et prend un trade
        sur les extrêmes (haut/bas du range).
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

        if price >= top_zone:
            return {
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
            return {
                "action": "BUY",
                "asset": asset,
                "entry_price": price,
                "target_sl_pips": float(cfg.get("sl_pips", 30)),
                "target_tp_pips": float(cfg.get("tp_pips", 30)),
                "rule_name": "range_accumulation_low",
                "strategy_type": "scalping",
                "confidence": 0.7,
            }
        return None

    # === Règle Marubozu / Impulsion ===
    def _rule_marubozu_impulse(
        self, df: pd.DataFrame, asset: str, price: float, cfg: Dict[str, Any]
    ) -> Optional[Dict[str, Any]]:
        """
        Détecte une bougie marubozu (longue bougie sans mèches)
        et prend un trade dans sa direction.
        """
        if len(df) < 2:
            return None

        last = df.iloc[-1]
        size = last["high"] - last["low"]
        body = abs(last["close"] - last["open"])
        upper_wick = last["high"] - max(last["open"], last["close"])
        lower_wick = min(last["open"], last["close"]) - last["low"]

        # critères marubozu
        if (
            body
            > cfg.get("min_body_mult", 2.5)
            * df["close"].diff().rolling(20).std().iloc[-1]
        ):
            if upper_wick < 0.1 * body and lower_wick < 0.1 * body:
                action = "BUY" if last["close"] > last["open"] else "SELL"
                return {
                    "action": action,
                    "asset": asset,
                    "entry_price": price,
                    "target_sl_pips": float(cfg.get("sl_pips", 40)),
                    "target_tp_pips": float(cfg.get("tp_pips", 80)),
                    "rule_name": "marubozu_impulse",
                    "strategy_type": "scalping",
                    "confidence": 0.8,
                }
        return None

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
