# scalping.py — version Burst-Only (no Bollinger / no Katana)
from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple
import math
import time
import uuid

import numpy as np
import pandas as pd


class ScalpingStrategy:
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

    # ==========================================================
    # =============   API PRINCIPALE (ENTRÉE)   ================
    # ==========================================================
    def evaluate_entry(
        self,
        asset: str,
        market_df: Optional[pd.DataFrame],
        signals: Dict[str, Any],
        context: Dict[str, Any],
        config: Dict[str, Any],
    ) -> Dict[str, Any]:
        """
        Décide d’une entrée scalping.
        Retourne soit un trade unique, soit un panier 'burst_decisions' prêt pour l’exécution.

        Signature attendue par le StrategyManager / DecisionPipeline :
          - "action": "BUY"/"SELL"
          - "asset": str
          - "entry_price": float
          - (optionnel) "sl_price"/"tp_price" OU "target_sl_pips"/"target_tp_pips"
          - (burst) {"burst_decisions": [ ... ]} avec un "basket_id"
        """
        
        # Vérifier que l'actif est autorisé pour scalping
        allowed_assets = config.get("tradeable_assets", [])
        if allowed_assets and asset not in allowed_assets:
            self.logger.info(f"[{asset}] Ignoré: non autorisé pour scalping (whitelist={allowed_assets}).")
            return {}

        # 0) Garde globales simples (news, disponibilité prix, spread)
        if config.get("halt_on_major_news", True) and self._has_blocking_news(context):
            self.logger.info(f"[{asset}] Halt: actualité majeure.")
            return {}

        price = self._safe_price_from_signals(signals)
        if not price:
            self.logger.warning(f"[{asset}] Prix invalide/absent pour evaluate_entry.")
            return {}

        # Symbol meta (robuste)
        meta = self._safe_asset_meta(asset, signals, context, config)
        pip_size = meta["pip_size"]
        if pip_size <= 0:
            self.logger.warning(f"[{asset}] pip_size invalide.")
            return {}

        spread_pips = meta["spread_pips"]
        max_spread = float(config.get("entry_rules", {}).get("scalping", {}).get("max_spread_pips", 999))
        if spread_pips > max_spread:
            self.logger.info(f"[{asset}] Spread trop élevé ({spread_pips:.2f}p > {max_spread:.2f}p).")
            return {}

        # 1) Direction de base : MTF > Phase
        action = self._infer_action_from_signals(signals)
        if action is None:
            self.logger.info(f"[{asset}] Aucune direction claire (MTF/phase).")
            return {}

        # 2) Option: déclencheur Liquidity Sweep → peut forcer une action
        liq_cfg = (config.get("scalping") or {}).get("liquidity_sweep", {}) or {}
        if liq_cfg.get("enabled", True) and isinstance(market_df, pd.DataFrame) and len(market_df) >= int(liq_cfg.get("lookback_bars", 20)):
            liq_action = self._rule_liquidity_sweep(market_df, meta, lookback=int(liq_cfg.get("lookback_bars", 20)))
            if liq_action in {"BUY", "SELL"}:
                action = liq_action
                self.logger.info(f"[{asset}] Liquidity Sweep → action forcée = {action}")

        # 3) Mode BURST prioritaire
        burst_cfg = (config.get("burst_scalping") or {})
        if bool(burst_cfg.get("enabled", True)):
            burst = self._rule_burst_scalping(asset, action, price, meta, signals, burst_cfg, context)
            if burst:
                return burst

        # 4) Sinon, entrée simple (fallback propre sans Bollinger)
        #    -> On propose des cibles en pips (TP/SL) dynamiques (adaptées vol/ATR).
        sl_pips, tp_pips, regime_tag = self._dynamic_tp_sl_from_vol_atr(signals, meta, config)
        
        decision = {
            "action": action,
            "asset": asset,
            "entry_price": price,
            "target_sl_pips": float(round(sl_pips, 3)),
            "target_tp_pips": float(round(tp_pips, 3)),
            "rule_name": "scalping_simple",
            "strategy_type": "scalping",  # ✅ ajouté pour audit & logs
            "confidence": float(signals.get("confidence_stabilized", signals.get("confidence_score", 0.0) or 0.0)),
            "meta": {"regime_tag": regime_tag},
        }
        return decision


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
        if min_atr_m1 > 0 and float(signals.get("atr_m1", 0.0) or 0.0) / max(meta["pip_size"], 1e-12) < min_atr_m1:
            self.logger.info(f"[{asset}] Burst refusé: ATR M1 < {min_atr_m1} pips.")
            return None
        max_spread_burst = float(burst_cfg.get("max_spread_pips", 999))
        if meta["spread_pips"] > max_spread_burst:
            self.logger.info(f"[{asset}] Burst refusé: spread {meta['spread_pips']:.2f}p > {max_spread_burst}p.")
            return None

        # Confirmation directionnelle M1 (facultative)
        if burst_cfg.get("require_m1_bias", False):
            m1_bias = str(signals.get("m1_bias", "")).lower()
            if (action == "BUY" and m1_bias != "up") or (action == "SELL" and m1_bias != "down"):
                self.logger.info(f"[{asset}] Burst refusé: m1_bias={m1_bias} incompatible avec action={action}.")
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
                "strategy_type": "scalping",   # ✅ ajouté pour cohérence
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

        self.logger.info(f"[{asset}] 🔥 Burst Scalping: {size}x {action} @ {entry_price} | basket_id={basket_id}")
        return {"burst_decisions": decisions, "basket_id": basket_id}

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

        phase = str(signals.get("phase_memory_stabilized", signals.get("phase", ""))).lower()
        if any(k in phase for k in ["bull", "up", "accumulation", "expansion", "trend"]):
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

        spread_points = _num(md_asset.get("current_spread_points", signals.get("spread", 0.0)), 0.0)
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
    def _is_engulfing(o: float, h: float, l: float, c: float, oo: float, cc: float) -> bool:
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
        return (upper >= 2 * body and lower <= body) or (lower >= 2 * body and upper <= body)

    @staticmethod
    def _is_doji(o: float, c: float, h: float, l: float) -> bool:
        rng = h - l
        body = abs(c - o)
        return rng > 0 and (body / rng) <= 0.1
