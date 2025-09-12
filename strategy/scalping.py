# strategy/scalping.py

from typing import Dict, Any, List, Optional
from .base_strategy import BaseStrategy
import math


class ScalpingStrategy(BaseStrategy):

    def _rule_liquidity_sweep(self, asset, df, cfg_rule, context):
        """Independent entry rule: sweep of prior swing + absorption.
        Detects if the latest candle swept above/below the recent extremes and closed back inside.
        Config keys (defaults):
        lookback: 30, min_wick_frac: 0.55, min_body_frac: 0.20, absorb_close_back_in: True,
        prefer_direction: 'auto', min_confidence: 0.40, sl_pips: 6, tp_pips: 8.1
        """
        try:
            if df is None or len(df) < max(40, int(cfg_rule.get("lookback", 30)) + 3):
                return None
            lookback = int(cfg_rule.get("lookback", 30))
            min_wick_frac = float(cfg_rule.get("min_wick_frac", 0.55))
            min_body_frac = float(cfg_rule.get("min_body_frac", 0.20))
            min_conf = float(cfg_rule.get("min_confidence", 0.40))
            sl_pips = float(cfg_rule.get("sl_pips", 6.0))
            tp_pips = float(cfg_rule.get("tp_pips", 8.1))

            high = df["high"]
            low = df["low"]
            close = df["close"]
            open_ = df["open"]
            prev_high = high.iloc[-(lookback + 1) : -1].max()
            prev_low = low.iloc[-(lookback + 1) : -1].min()

            h = float(high.iloc[-1])
            l = float(low.iloc[-1])
            c = float(close.iloc[-1])
            o = float(open_.iloc[-1])
            rng = max(1e-12, h - l)
            body = abs(c - o)
            body_frac = body / rng if rng > 0 else 0.0
            upper_wick = max(0.0, h - max(c, o))
            lower_wick = max(0.0, min(c, o) - l)
            uw_frac = upper_wick / rng
            lw_frac = lower_wick / rng

            side = None
            conf = 0.0
            # Bearish sweep: take liquidity above previous highs, then close back below that level with strong upper wick
            if (
                h > prev_high
                and c < prev_high
                and uw_frac >= min_wick_frac
                and body_frac >= min_body_frac
            ):
                side = "SELL"
                # Confidence boosts with how far swept and wick/body quality
                sweep_amp = (h - prev_high) / max(1e-12, rng)
                conf = min(
                    1.0,
                    0.35
                    + 0.35 * min(1.0, sweep_amp)
                    + 0.15 * min(1.0, uw_frac)
                    + 0.15 * min(1.0, body_frac / min_body_frac),
                )
            # Bullish sweep: take liquidity below previous lows, then close back above that level with strong lower wick
            elif (
                l < prev_low
                and c > prev_low
                and lw_frac >= min_wick_frac
                and body_frac >= min_body_frac
            ):
                side = "BUY"
                sweep_amp = (prev_low - l) / max(1e-12, rng)
                conf = min(
                    1.0,
                    0.35
                    + 0.35 * min(1.0, sweep_amp)
                    + 0.15 * min(1.0, lw_frac)
                    + 0.15 * min(1.0, body_frac / min_body_frac),
                )

            if not side or conf < min_conf:
                return None

            order = {
                "action": "OPEN",
                "asset": asset,
                "side": side,
                "sl_pips": sl_pips,
                "tp_pips": tp_pips,
                "volume": cfg_rule.get("volume")
                or (self.strategy_config or {}).get("default_volume")
                or 0.01,
                "rule_name": f"scalping:liquidity_sweep:{asset}",
                "confidence": conf,
                "meta": {
                    "prev_high": float(prev_high),
                    "prev_low": float(prev_low),
                    "uw_frac": float(uw_frac),
                    "lw_frac": float(lw_frac),
                    "body_frac": float(body_frac),
                    "lookback": lookback,
                },
            }
            return order
        except Exception as e:
            try:
                self.logger.exception(f"Liquidity sweep rule failed for {asset}: {e}")
            except Exception:
                pass
            return None

    def _rule_midline_bollinger(self, asset, df, cfg_rule, context):
        """Independent entry rule: mean-revert vs Bollinger midline.
        Config keys (with defaults if missing):
        length: 20, k: 2.0, mode: 'mean_revert'|'momentum', epsilon_band_frac: 0.15,
        min_body_frac: 0.20, allow_counter_mtf: True, min_confidence: 0.35,
        sl_pips: 6, tp_pips: 8.1
        """
        try:
            if df is None or len(df) < max(30, int(cfg_rule.get("length", 20)) + 5):
                return None
            close = df["close"]
            open_ = df["open"]
            high = df["high"]
            low = df["low"]
            length = int(cfg_rule.get("length", 20))
            k = float(cfg_rule.get("k", 2.0))
            mode = (cfg_rule.get("mode") or "mean_revert").lower()
            eps_band = float(cfg_rule.get("epsilon_band_frac", 0.15))
            min_body_frac = float(cfg_rule.get("min_body_frac", 0.20))
            min_conf = float(cfg_rule.get("min_confidence", 0.35))
            sl_pips = float(cfg_rule.get("sl_pips", 6.0))
            tp_pips = float(cfg_rule.get("tp_pips", 8.1))

            mid = self._sma(close, length)
            std = self._std(close, length)
            if mid is None or std is None:
                return None
            bw = std * k  # half-band "strength"
            last_mid = mid.iloc[-1]
            last_bw = float(bw.iloc[-1] or 0.0)
            last_close = float(close.iloc[-1])
            last_open = float(open_.iloc[-1])
            last_high = float(high.iloc[-1])
            last_low = float(low.iloc[-1])
            rng = max(1e-12, last_high - last_low)
            body = abs(last_close - last_open)
            body_frac = body / rng if rng > 0 else 0.0

            if last_bw <= 0:
                return None

            # Distance to midline, normalized by band width
            dist_norm = (last_close - last_mid) / last_bw

            side = None
            # Mean-revert: trade back toward the midline when price is outside/near bands
            if mode.startswith("mean"):
                # If price is below midline (negative dist), prefer BUY; above -> SELL.
                if dist_norm <= -eps_band:
                    side = "BUY"
                elif dist_norm >= eps_band:
                    side = "SELL"
            else:
                # Momentum: follow direction away from midline with body confirmation
                if dist_norm >= eps_band and last_close > last_open:
                    side = "BUY"
                elif dist_norm <= -eps_band and last_close < last_open:
                    side = "SELL"

            if not side:
                return None

            # Confidence: combine distance & body quality
            conf = min(
                1.0,
                max(
                    0.0,
                    0.5 * min(1.0, abs(dist_norm))
                    + 0.5 * min(1.0, body_frac / max(1e-6, min_body_frac)),
                ),
            )
            if conf < min_conf:
                return None

            # Build order
            meta = self._safe_asset_meta(context, asset)
            order = {
                "action": "OPEN",
                "asset": asset,
                "side": side,
                "sl_pips": sl_pips,
                "tp_pips": tp_pips,
                "volume": cfg_rule.get("volume")
                or (self.strategy_config or {}).get("default_volume")
                or 0.01,
                "rule_name": f"scalping:midline_bollinger:{asset}",
                "confidence": conf,
                "meta": {
                    "dist_norm": float(dist_norm),
                    "band_width": float(last_bw),
                    "body_frac": float(body_frac),
                    "length": length,
                    "k": k,
                },
            }
            return order
        except Exception as e:
            try:
                self.logger.exception(f"Midline rule failed for {asset}: {e}")
            except Exception:
                pass
            return None

    def _atr(self, df, length: int = 14):
        try:
            high = df["high"]
            low = df["low"]
            close = df["close"]
            prev_close = close.shift(1)
            tr = (high - low).abs()
            tr = tr.combine((high - prev_close).abs(), max)
            tr = tr.combine((low - prev_close).abs(), max)
            return tr.rolling(int(length)).mean()
        except Exception:
            return None

    def _std(self, series, length: int):
        try:
            return series.rolling(int(length)).std(ddof=0)
        except Exception:
            return None

    def _sma(self, series, length: int):
        try:
            return series.rolling(int(length)).mean()
        except Exception:
            return None

    def _safe_asset_meta(self, context, asset):
        meta = {}
        try:
            # Try from context.asset_configs first
            acfg = ((context or {}).get("asset_configs") or {}).get(asset) or {}
            if isinstance(acfg, dict):
                meta["digits"] = acfg.get("digits")
                meta["point"] = acfg.get("point")
            # Try open_positions meta if available
            pos = ((context or {}).get("open_positions") or {}).get(asset) or {}
            meta["digits"] = meta.get("digits") or pos.get("digits")
            meta["point"] = meta.get("point") or pos.get("point")
        except Exception:
            pass
        # Fallbacks
        if not meta.get("digits"):
            meta["digits"] = 5 if asset.endswith(("USD", "CHF")) else 3
        if not meta.get("point"):
            # default MetaTrader 'point' for 5-digit FX pairs
            meta["point"] = 1e-5 if meta["digits"] >= 5 else 0.001
        return meta

        # ------------------------------
        # 🔎 Lecture de chandeliers
        # ------------------------------

    def _is_engulfing(self, df, bullish=True):
        """Détecte un avalement haussier ou baissier (engulfing)."""
        if len(df) < 2:
            return False
        prev_o, prev_c = df["open"].iloc[-2], df["close"].iloc[-2]
        last_o, last_c = df["open"].iloc[-1], df["close"].iloc[-1]

        if bullish:
            return (
                prev_c < prev_o
                and last_c > last_o
                and last_c > prev_o
                and last_o < prev_c
            )
        else:
            return (
                prev_c > prev_o
                and last_c < last_o
                and last_c < prev_o
                and last_o > prev_c
            )

    def _is_pinbar(self, df, bullish=True, min_wick_ratio=2.0):
        """Détecte un pin bar (longue mèche rejet)."""
        if len(df) < 1:
            return False
        o, c, h, l = (
            df["open"].iloc[-1],
            df["close"].iloc[-1],
            df["high"].iloc[-1],
            df["low"].iloc[-1],
        )
        body = abs(c - o)
        upper_wick = h - max(o, c)
        lower_wick = min(o, c) - l
        if bullish:
            return lower_wick > body * min_wick_ratio
        else:
            return upper_wick > body * min_wick_ratio

    def _is_doji(self, df, max_body_frac=0.1):
        """Détecte un doji (indécision)."""
        if len(df) < 1:
            return False
        o, c, h, l = (
            df["open"].iloc[-1],
            df["close"].iloc[-1],
            df["high"].iloc[-1],
            df["low"].iloc[-1],
        )
        rng = h - l
        body = abs(c - o)
        return rng > 0 and (body / rng) < max_body_frac

        """
        Stratégie de Scalping pour SNIPER_X.
        - Choisit le meilleur actif parmi les signaux fournis par le DecisionPipeline
        sans re-filtrer les conditions déjà validées en amont.
        - Gère la sortie de position de façon proactive lorsque le momentum s'estompe.
        """

    # ---------------------------------------------------------------------
    # Initialisation & utilitaires
    # ---------------------------------------------------------------------
    def __init__(self, config_manager_instance: Any, strategy_config: Dict[str, Any]):
        """Initialise la stratégie en chargeant les paramètres clés."""
        super().__init__(config_manager_instance, strategy_config)
        self._refresh_from_config()
        # Log neutre (suppression de toute référence au momentum fade)
        self.logger.info(
            "ScalpingStrategy initialisée | magic=%s | assets=%d | TP=%.2f pips | SL=%.2f pips",
            self.magic_number,
            len(self.tradeable_assets or []),
            getattr(self, "take_profit_pips", float("nan")),
            getattr(self, "stop_loss_pips", float("nan")),
        )

    def _refresh_from_config(self) -> None:
        """Recharge les attributs dérivés de la configuration courante (sans momentum fade)."""
        # --- Paramètres essentiels ---
        self.tradeable_assets: List[str] = list(
            self.strategy_config.get("tradeable_assets", [])
        )
        self.magic_number = self.strategy_config.get("magic_number")

        # Valeurs par défaut raisonnables si absentes
        try:
            self.take_profit_pips = float(
                self.strategy_config.get("take_profit_pips", 10)
            )
        except Exception:
            self.take_profit_pips = 10.0

        try:
            self.stop_loss_pips = float(self.strategy_config.get("stop_loss_pips", 8))
        except Exception:
            self.stop_loss_pips = 8.0

    def _infer_action_from_signals(
        self, asset_signals: Dict[str, Any]
    ) -> Optional[str]:
        """
        Détermine l'action BUY/SELL à partir de différentes clés communes.
        Ordre de priorité :
        1) 'action' déjà normalisé (BUY/SELL)
        2) 'direction' ou 'trend' (up/down, bullish/bearish)
        3) signe de 'volume_momentum' (>0 => BUY, <0 => SELL)
        """
        # 1) Action explicite
        action = (asset_signals.get("action") or "").upper()
        if action in {"BUY", "SELL"}:
            return action

        # 2) Direction textuelle
        direction = (
            asset_signals.get("direction") or asset_signals.get("trend") or ""
        ).lower()
        if any(k in direction for k in ("up", "bull", "bullish", "long")):
            return "BUY"
        if any(k in direction for k in ("down", "bear", "bearish", "short")):
            return "SELL"

        # 3) Momentum signé
        try:
            vm = float(asset_signals.get("volume_momentum", 0.0))
            if vm > 0:
                return "BUY"
            if vm < 0:
                return "SELL"
        except Exception:
            pass

        # 4) Phase (moins fiable car souvent neutre)
        phase = (asset_signals.get("phase") or "").lower()
        if any(k in phase for k in ("expansion_up", "up", "bull")):
            return "BUY"
        if any(k in phase for k in ("expansion_down", "down", "bear")):
            return "SELL"

        return None

    def evaluate_entry(
        self, context: Dict[str, Any], signals: Dict[str, Any]
    ) -> Optional[Dict[str, Any]]:
        """
        Logique d'entrée Katana (micro-phase, SL/TP serrés) — SANS GATING DUR.
        - Sélectionne le meilleur actif parmi self.tradeable_assets
        - Scoring purement soft (OB/FVG/MTF/break M1 en BONUS/PÉNALITÉ), pas de hard-block
        - Ne refait PAS les vérifications de marché (spread, horaires) ici
        """
        self.logger.debug(
            "ScalpingStrategy: évaluation d'entrée (Katana, no-gating)..."
        )

        # === NEW MULTI-RULE PRECHECKS ===
        rules_cfg = (self.strategy_config or {}).get("entry_rules") or {}
        ls_cfg = (
            (rules_cfg.get("liquidity_sweep") or {})
            if isinstance(rules_cfg, dict)
            else {}
        )
        mb_cfg = (
            (rules_cfg.get("midline_bollinger") or {})
            if isinstance(rules_cfg, dict)
            else {}
        )

        candidates = []

        # 🔧 Correction : définir market_data à partir du context
        market_data = context.get("market_data") or {}

        def try_rule_over_assets(rule_fn, rcfg, rule_name_hint):
            best = None
            for asset, df in market_data.items():
                if df is None or not hasattr(df, "iloc"):
                    continue
                try:
                    order = rule_fn(asset, df, rcfg, context)
                    if order:
                        if (best is None) or (
                            order.get("confidence", 0.0) > best.get("confidence", 0.0)
                        ):
                            best = order
                except Exception:
                    pass
            if best:
                candidates.append(best)

        if bool(ls_cfg.get("enabled", True)):
            try_rule_over_assets(self._rule_liquidity_sweep, ls_cfg, "liquidity_sweep")

        if bool(mb_cfg.get("enabled", True)):
            try_rule_over_assets(
                self._rule_midline_bollinger, mb_cfg, "midline_bollinger"
            )

        if candidates:
            best_cand = sorted(
                candidates, key=lambda x: x.get("confidence", 0.0), reverse=True
            )[0]
            best_cand["action"] = "OPEN"
            return best_cand
        # === END MULTI-RULE PRECHECKS ===

        # --- Raccourcis config ---
        cfg = self.strategy_config or {}
        entry_rules = (cfg.get("entry_rules") or {}).get("scalping") or {}
        dec_eng = cfg.get("decision_engine") or {}
        scoring_cfg = dec_eng.get("scoring") or {}

        min_conf = float(entry_rules.get("min_confidence", 0.30) or 0.30)

        mtf_bonus_per_hit = float(scoring_cfg.get("mtf_bonus_per_hit", 0.10) or 0.10)
        liquidity_bonus = float(scoring_cfg.get("liquidity_bonus", 0.10) or 0.10)
        ob_conf_bonus = float(scoring_cfg.get("ob_conf_bonus", 0.25) or 0.25)
        base_bias = float(scoring_cfg.get("strategy_bias", 0.30) or 0.30)
        high_spread_penalty = float(
            scoring_cfg.get("high_spread_penalty", -0.10) or -0.10
        )
        low_vol_penalty = float(
            scoring_cfg.get("global_low_vol_penalty", -0.20) or -0.20
        )

        m1_break_bonus = float(entry_rules.get("m1_break_bonus", 0.12) or 0.12)
        mtf_min_hits_target = int(
            (dec_eng.get("mtf") or {}).get("required_agreements", 1) or 1
        )
        mtf_shortfall_penalty = float(
            scoring_cfg.get("mtf_shortfall_penalty", -0.08) or -0.08
        )

        def _bool(x, key: str) -> bool:
            v = (x or {}).get(key)
            return bool(v is True or str(v).lower() in ("true", "1", "yes"))

        def _float(x, key: str, default: float = 0.0) -> float:
            try:
                return float((x or {}).get(key, default))
            except Exception:
                return float(default)

        def _get_confidence(s: Dict[str, Any]) -> float:
            for k in ("confidence", "confidence_score", "score", "final_confidence"):
                v = s.get(k)
                if isinstance(v, (int, float)):
                    return float(v)
            return 0.0

        def _get_mtf_hits(s: Dict[str, Any]) -> int:
            for k in ("mtf_hits", "mtf_agreements", "mtf_confluence"):
                v = s.get(k)
                try:
                    iv = int(v)
                    if iv >= 0:
                        return iv
                except Exception:
                    pass
            hits = 0
            for k in ("m1_align", "m5_align", "m15_align"):
                if _bool(s, k):
                    hits += 1
            return hits

        spreads = context.get("spreads_pips") or {}

        best_asset: Optional[str] = None
        best_score: float = float("-inf")
        best_debug: Dict[str, Any] = {}

        for asset in self.tradeable_assets:
            s = signals.get(asset) or {}
            if not s:
                continue

            confidence = _get_confidence(s)
            if confidence < min_conf:
                continue

            ob = (
                _bool(s, "ob_detected")
                or _bool(s, "order_block")
                or _bool(s, "order_block_ml_enhanced")
            )
            fvg = _bool(s, "fvg_detected") or _bool(s, "fvg_enhanced")
            m1_break = _bool(s, "m1_break") or _bool(s, "bos_mss_enhanced")

            mtf_hits = _get_mtf_hits(s)

            spread_pips = (
                float(spreads.get(asset, 0.0))
                if isinstance(spreads.get(asset), (int, float))
                else 0.0
            )
            vol_z = _float(s, "volume_zscore", 0.0)
            low_vol = vol_z < 0.0

            score = 0.0
            score += confidence
            score += base_bias
            if ob:
                score += ob_conf_bonus
            if fvg:
                score += ob_conf_bonus * 0.6
            if m1_break:
                score += m1_break_bonus

            score += mtf_hits * mtf_bonus_per_hit
            if mtf_hits < mtf_min_hits_target:
                score += mtf_shortfall_penalty

            if _bool(s, "liquidity_ok") or _bool(s, "liquidity_grab_detected"):
                score += liquidity_bonus

            if spread_pips and spread_pips > _float(
                entry_rules, "max_spread_pips", 2.0
            ):
                score += high_spread_penalty
            if low_vol:
                score += low_vol_penalty

            # 🔎 Lecture chandeliers
            df = market_data.get(asset)
            if df is not None and hasattr(df, "iloc") and len(df) > 2:
                if self._is_engulfing(df, bullish=(confidence >= 0.5)):
                    score += 0.15
                if self._is_pinbar(df, bullish=(confidence >= 0.5)):
                    score += 0.10
                if self._is_doji(df):
                    score -= 0.20

            if score > best_score:
                best_score = score
                best_asset = asset
                best_debug = {
                    "confidence": confidence,
                    "ob": ob,
                    "fvg": fvg,
                    "m1_break": m1_break,
                    "mtf_hits": mtf_hits,
                    "spread_pips": spread_pips,
                    "volume_zscore": vol_z,
                    "score": score,
                }

        if not best_asset:
            return None

        action = self._infer_action_from_signals(signals.get(best_asset, {}) or {})
        if action is None:
            return None

        order = {
            "action": action,
            "asset": best_asset,
            "order_type": "MARKET",
            "strategy_type": cfg.get("strategy_name", "scalping"),
            "rule_name": f"scalping:katana:{best_asset}",
            "magic_number": self.magic_number,
            "target_tp_pips": self.take_profit_pips,
            "target_sl_pips": self.stop_loss_pips,
            "comment": "SNIPER_X:scalping_katana_no_gating",
        }
        return order

        # ---------------------------------------------------------------------

    # Sorties Katana (micro-phase, serrées) — pas de momentum fade
    # ---------------------------------------------------------------------
    def _pips_between(self, a: float, b: float, point: float, digits: int) -> float:
        """Calcule |a-b| en pips selon digits (3/5 -> 10 points = 1 pip)."""
        pip_points = 10.0 if digits in (3, 5) else 1.0
        try:
            return abs(float(a) - float(b)) / (float(point) * pip_points)
        except Exception:
            return 0.0

    def evaluate_exit(
        self,
        context: Dict[str, Any],
        position: Dict[str, Any],
        latest_signals: Dict[str, Any] | None = None,
    ) -> Optional[Dict[str, Any]]:
        """
        Propose une sortie partielle/totale pour une position ouverte (ou un ajustement de SL) selon des règles Katana + chandeliers.
        - Retourne:
            • {"action":"ADJUST_SL", "asset":..., "new_sl_price":...} OU
            • {"action":"CLOSE", "asset":..., "reason": "..."} OU
            • None si aucune action.
        """
        cfg = self.strategy_config or {}
        exit_cfg = (cfg.get("exit_rules") or {}).get("scalping") or {}

        asset = str(position.get("symbol") or position.get("asset") or "").upper()
        if not asset:
            return None

        md = (context.get("market_data") or {}).get(asset, {}) or {}
        symbol_info = md.get("symbol_info", {}) or {}
        point = float(symbol_info.get("point", 0.00001) or 0.00001)
        digits = int(symbol_info.get("digits", 5))

        side = str(
            position.get("action") or position.get("type") or position.get("side") or ""
        ).upper()
        if side not in ("BUY", "SELL"):
            return None

        entry_price = float(
            position.get("entry_price") or position.get("price_open") or 0.0
        )
        sl_price = float(position.get("sl") or 0.0) or None
        tp_price = float(position.get("tp") or 0.0) or None
        cur_price = float(
            md.get("current_price") or position.get("price_current") or 0.0
        )
        if entry_price <= 0 or cur_price <= 0 or point <= 0:
            return None

        # --- PnL courant en pips (non signé et signé) ---
        pnl_pips_abs = self._pips_between(cur_price, entry_price, point, digits)
        if side == "BUY":
            pnl_pips_signed = (cur_price - entry_price) / (
                point * (10.0 if digits in (3, 5) else 1.0)
            )
        else:
            pnl_pips_signed = (entry_price - cur_price) / (
                point * (10.0 if digits in (3, 5) else 1.0)
            )

        # --- Paramètres de sortie (avec defaults conservateurs) ---
        breakeven_trigger = float(exit_cfg.get("breakeven_trigger_pips", 3.0))
        trailing_start = float(exit_cfg.get("trailing_start_pips", 5.0))
        trailing_step = float(exit_cfg.get("trailing_step_pips", 1.0))
        max_hold_seconds = int(exit_cfg.get("max_hold_seconds", 0))  # 0 = désactivé
        exit_on_m1_flip = bool(exit_cfg.get("exit_on_m1_phase_flip", True))

        # Derniers signaux/phase pour l’asset
        s = (latest_signals or {}).get(asset, {}) if latest_signals else {}
        phase_m1 = str(s.get("phase_m1") or s.get("phase") or "").lower()

        # --- 1) Break-even auto ---
        if breakeven_trigger > 0 and pnl_pips_signed >= breakeven_trigger:
            be_pad = float(exit_cfg.get("breakeven_pad_pips", 0.1))
            new_sl = entry_price
            if side == "BUY":
                new_sl = min(
                    cur_price,
                    entry_price + be_pad * point * (10.0 if digits in (3, 5) else 1.0),
                )
                if not sl_price or new_sl > sl_price:
                    return {
                        "action": "ADJUST_SL",
                        "asset": asset,
                        "new_sl_price": new_sl,
                        "reason": "breakeven",
                    }
            else:
                new_sl = max(
                    cur_price,
                    entry_price - be_pad * point * (10.0 if digits in (3, 5) else 1.0),
                )
                if not sl_price or new_sl < sl_price:
                    return {
                        "action": "ADJUST_SL",
                        "asset": asset,
                        "new_sl_price": new_sl,
                        "reason": "breakeven",
                    }

        # --- 2) Trailing léger par pas ---
        if (
            trailing_start > 0
            and trailing_step > 0
            and pnl_pips_signed >= trailing_start
        ):
            step_buffer = float(exit_cfg.get("trailing_buffer_pips", trailing_step))
            target_lock = max(0.0, pnl_pips_signed - step_buffer)
            lock_dist_price = target_lock * point * (10.0 if digits in (3, 5) else 1.0)
            new_sl = (
                entry_price + lock_dist_price
                if side == "BUY"
                else entry_price - lock_dist_price
            )
            if side == "BUY":
                if not sl_price or new_sl > sl_price:
                    new_sl = min(new_sl, cur_price)
                    return {
                        "action": "ADJUST_SL",
                        "asset": asset,
                        "new_sl_price": new_sl,
                        "reason": "trail",
                    }
            else:
                if not sl_price or new_sl < sl_price:
                    new_sl = max(new_sl, cur_price)
                    return {
                        "action": "ADJUST_SL",
                        "asset": asset,
                        "new_sl_price": new_sl,
                        "reason": "trail",
                    }

        # --- 3) Flip micro-phase M1 (optionnel, sortie totale) ---
        if exit_on_m1_flip and phase_m1:
            if side == "BUY" and any(
                k in phase_m1
                for k in ("down", "bear", "expansion_down", "distribution")
            ):
                return {
                    "action": "CLOSE",
                    "asset": asset,
                    "reason": "m1_phase_flip_against",
                }
            if side == "SELL" and any(
                k in phase_m1 for k in ("up", "bull", "expansion_up", "accumulation")
            ):
                return {
                    "action": "CLOSE",
                    "asset": asset,
                    "reason": "m1_phase_flip_against",
                }

        # --- 4) Durée max (optionnel) ---
        if max_hold_seconds and max_hold_seconds > 0:
            import datetime as _dt

            opened_at = (
                position.get("time")
                or position.get("time_open")
                or position.get("open_time")
            )
            try:
                if isinstance(opened_at, (int, float)):
                    open_dt = _dt.datetime.utcfromtimestamp(opened_at)
                else:
                    open_dt = _dt.datetime.fromisoformat(
                        str(opened_at).replace("Z", "+00:00")
                    ).astimezone(_dt.timezone.utc)
                now_utc = _dt.datetime.fromisoformat(
                    str(context.get("current_time_utc"))
                ).astimezone(_dt.timezone.utc)
                held = (now_utc - open_dt).total_seconds()
                if held >= max_hold_seconds:
                    return {
                        "action": "CLOSE",
                        "asset": asset,
                        "reason": "max_hold_time",
                    }
            except Exception:
                pass

        # --- 5) Sortie technique par chandeliers ---
        df = (context.get("market_data") or {}).get(asset, {}).get("annotated_rates_df")
        if df is not None and hasattr(df, "iloc") and len(df) > 2:
            if side == "BUY":
                if self._is_engulfing(df, bullish=False) or self._is_pinbar(
                    df, bullish=False
                ):
                    return {
                        "action": "CLOSE",
                        "asset": asset,
                        "reason": "bearish_candle_pattern",
                    }
            elif side == "SELL":
                if self._is_engulfing(df, bullish=True) or self._is_pinbar(
                    df, bullish=True
                ):
                    return {
                        "action": "CLOSE",
                        "asset": asset,
                        "reason": "bullish_candle_pattern",
                    }

            # Doji en profit = sécuriser
            if self._is_doji(df) and pnl_pips_signed > breakeven_trigger:
                return {
                    "action": "ADJUST_SL",
                    "asset": asset,
                    "new_sl_price": entry_price,
                    "reason": "doji_uncertainty",
                }

        return None

    def get_parameters(self) -> Dict[str, Any]:
        """Retourne une copie des paramètres de configuration de la stratégie."""
        return dict(self.strategy_config)

    def update_strategy_parameters(self, new_params: Dict[str, Any]) -> None:
        """
        Met à jour la configuration de la stratégie proprement.
        - Merge profond des dictionnaires
        - Rafraîchit les attributs dépendants de la config
        """
        self.logger.info("Mise à jour des paramètres Scalping: %s", new_params)

        def _deep_merge(dst: Dict[str, Any], src: Dict[str, Any]) -> Dict[str, Any]:
            for k, v in (src or {}).items():
                if isinstance(v, dict) and isinstance(dst.get(k), dict):
                    dst[k] = _deep_merge(dst.get(k, {}), v)
                else:
                    dst[k] = v
            return dst

        _deep_merge(self.strategy_config, new_params or {})
        self._refresh_from_config()
