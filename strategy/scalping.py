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
        Logique d'entrée Scalping STRICTE + filtres intelligents :
        - Liquidity Sweep
        - Midline Bollinger
        - Lecture chandeliers + EMA + séquence de bougies
        - PAS de fallback Katana : pas de signal => pas de trade
        """
        self.logger.debug("ScalpingStrategy: évaluation d'entrée (STRICTE + filtres)...")

        cfg = self.strategy_config or {}
        rules_cfg = (cfg.get("entry_rules") or {}).get("scalping") or {}
        ls_cfg = (rules_cfg.get("liquidity_sweep") or {})
        mb_cfg = (rules_cfg.get("midline_bollinger") or {})

        candidates = []
        market_data = context.get("market_data") or {}

        # === Liquidity Sweep ===
        if ls_cfg.get("enabled", True):
            for asset, df in market_data.items():
                if df is None or not hasattr(df, "iloc"):
                    continue
                try:
                    order = self._rule_liquidity_sweep(asset, df, ls_cfg, context)
                    if order:
                        candidates.append(order)
                except Exception as e:
                    self.logger.debug(f"Erreur liquidity_sweep {asset}: {e}")

        # === Midline Bollinger ===
        if mb_cfg.get("enabled", True):
            for asset, df in market_data.items():
                if df is None or not hasattr(df, "iloc"):
                    continue
                try:
                    order = self._rule_midline_bollinger(asset, df, mb_cfg, context)
                    if order:
                        candidates.append(order)
                except Exception as e:
                    self.logger.debug(f"Erreur midline_bollinger {asset}: {e}")

        if not candidates:
            self.logger.info("Aucun signal strict trouvé → pas de trade.")
            return None

        # === Sélection du meilleur signal ===
        best_cand = sorted(
            candidates, key=lambda x: x.get("confidence", 0.0), reverse=True
        )[0]

        # === Application des filtres intelligents ===
        df = market_data.get(best_cand["asset"])
        if df is not None and hasattr(df, "iloc") and len(df) > 20:
            close = df["close"]
            ema_fast = close.ewm(span=20).mean().iloc[-1]
            ema_slow = close.ewm(span=50).mean().iloc[-1]
            last_close = close.iloc[-1]

            # Filtre tendance
            if best_cand["side"] == "BUY" and not (last_close > ema_fast > ema_slow):
                self.logger.info("Signal rejeté: BUY mais tendance pas confirmée (EMA).")
                return None
            if best_cand["side"] == "SELL" and not (last_close < ema_fast < ema_slow):
                self.logger.info("Signal rejeté: SELL mais tendance pas confirmée (EMA).")
                return None

            # Séquence bougies
            last3 = close.iloc[-3:]
            if best_cand["side"] == "BUY" and not all(x < y for x, y in zip(last3, last3[1:])):
                self.logger.info("Signal rejeté: BUY sans séquence haussière claire.")
                return None
            if best_cand["side"] == "SELL" and not all(x > y for x, y in zip(last3, last3[1:])):
                self.logger.info("Signal rejeté: SELL sans séquence baissière claire.")
                return None

            # Chandeliers (engulfing, pinbar, doji)
            if best_cand["side"] == "BUY":
                if self._is_engulfing(df, bullish=False) or self._is_pinbar(df, bullish=False):
                    self.logger.info("Signal rejeté: pattern baissier contre BUY.")
                    return None
            if best_cand["side"] == "SELL":
                if self._is_engulfing(df, bullish=True) or self._is_pinbar(df, bullish=True):
                    self.logger.info("Signal rejeté: pattern haussier contre SELL.")
                    return None
            if self._is_doji(df):
                self.logger.info("Signal rejeté: doji détecté (indécision).")
                return None

        self.logger.info(
            "Signal retenu: %s | conf=%.3f | side=%s",
            best_cand["asset"],
            best_cand["confidence"],
            best_cand.get("side"),
        )
        return best_cand


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
