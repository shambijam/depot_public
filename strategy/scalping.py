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
from trader.sizing import _calculate_risk_based_volume
from phase_observer.footprint_analyzer import FootprintAnalyzer


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
       

        # Initialisation des détecteurs
        self.detectors = Detectors(logger=self.logger, config_manager=config_manager)

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
            df_m1 = ctx_md.get("df_m1") or ctx_md.get("df")
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
                    delta = fp_summary.get("delta_total")
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
                        d = fp_summary.get("delta_total")
                        if isinstance(d, (int, float)):
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

            # Config single_master
            sm_cfg = ((strat_cfg.get("entry_rules") or {}).get("scalping") or {}).get(
                "burst_single_master", {}
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

            # Décision single_master (aucun volume/SL ici → calculés en aval dans prepare_order)
            entry_mode = str(sm_cfg.get("entry_mode", "MARKET")).upper()
            burst_sz = int(sm_cfg.get("burst_size", 5) or 5)

            sm_decision = {
                "strategy_type": "scalping",
                "rule_name": "burst_single_master",
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

    def _rule_burst_single_master(
        self,
        asset: str,
        action: str,
        entry_price: float,
        meta: Dict[str, Any],
        sm_cfg: Dict[str, Any],
        context: Dict[str, Any],
    ) -> Optional[Dict[str, Any]]:
        """
        Single-Master:
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
            "rule_name": "burst_single_master",
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
        if df is None or len(df) < period + 2:
            return float("nan")
        h = df["high"].astype(float)
        l = df["low"].astype(float)
        c = df["close"].astype(float)
        pc = c.shift(1)
        tr = np.maximum.reduce([(h - l).abs(), (h - pc).abs(), (l - pc).abs()])
        atr = tr.rolling(window=period, min_periods=period).mean().iloc[-1]
        return float(atr) if pd.notna(atr) and atr > 0 else float("nan")

  