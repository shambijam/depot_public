# strategy/scalping.py

from typing import Dict, Any, List, Optional
from .base_strategy import BaseStrategy
import math


class ScalpingStrategy(BaseStrategy):
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
        self.tradeable_assets: List[str] = list(self.strategy_config.get("tradeable_assets", []))
        self.magic_number = self.strategy_config.get("magic_number")

        # Valeurs par défaut raisonnables si absentes
        try:
            self.take_profit_pips = float(self.strategy_config.get("take_profit_pips", 10))
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
        self.logger.debug("ScalpingStrategy: évaluation d'entrée (Katana, no-gating)...")

        # --- Raccourcis config ---
        cfg = self.strategy_config or {}
        entry_rules = ((cfg.get("entry_rules") or {}).get("scalping") or {})
        dec_eng = (cfg.get("decision_engine") or {})
        scoring_cfg = (dec_eng.get("scoring") or {})

        # Seul seuil "dur" conservé : confiance minimale
        min_conf = float(entry_rules.get("min_confidence", 0.30) or 0.30)

        # poids/bonus par défaut (fallback si absents de la config)
        mtf_bonus_per_hit = float(scoring_cfg.get("mtf_bonus_per_hit", 0.10) or 0.10)
        liquidity_bonus = float(scoring_cfg.get("liquidity_bonus", 0.10) or 0.10)
        ob_conf_bonus = float(scoring_cfg.get("ob_conf_bonus", 0.25) or 0.25)
        base_bias = float(scoring_cfg.get("strategy_bias", 0.30) or 0.30)
        high_spread_penalty = float(scoring_cfg.get("high_spread_penalty", -0.10) or -0.10)
        low_vol_penalty = float(scoring_cfg.get("global_low_vol_penalty", -0.20) or -0.20)

        # bonus soft spécifiques (remplacent l'ancien gating)
        m1_break_bonus = float(entry_rules.get("m1_break_bonus", 0.12) or 0.12)
        mtf_min_hits_target = int((dec_eng.get("mtf") or {}).get("required_agreements", 1) or 1)
        mtf_shortfall_penalty = float(scoring_cfg.get("mtf_shortfall_penalty", -0.08) or -0.08)

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

        spreads = (context.get("spreads_pips") or {})

        best_asset: Optional[str] = None
        best_score: float = float("-inf")
        best_debug: Dict[str, Any] = {}

        for asset in self.tradeable_assets:
            s = signals.get(asset) or {}
            if not s:
                continue

            # 1) Seuil de confiance (unique filtre dur)
            confidence = _get_confidence(s)
            if confidence < min_conf:
                self.logger.debug("Asset %s rejeté (confidence %.3f < %.3f).", asset, confidence, min_conf)
                continue

            # 2) Indices de confluence (SOFT)
            ob = _bool(s, "ob_detected") or _bool(s, "order_block") or _bool(s, "order_block_ml_enhanced")
            fvg = _bool(s, "fvg_detected") or _bool(s, "fvg_enhanced")
            m1_break = _bool(s, "m1_break") or _bool(s, "bos_mss_enhanced")

            mtf_hits = _get_mtf_hits(s)

            # 3) Features marché (SOFT)
            spread_pips = float(spreads.get(asset, 0.0)) if isinstance(spreads.get(asset), (int, float)) else 0.0
            vol_z = _float(s, "volume_zscore", 0.0)
            low_vol = vol_z < 0.0

            # 4) Scoring Katana (100% soft)
            score = 0.0
            score += confidence
            score += base_bias
            if ob:
                score += ob_conf_bonus
            if fvg:
                score += ob_conf_bonus * 0.6  # FVG un peu moins pondéré que OB
            if m1_break:
                score += m1_break_bonus

            # MTF : bonus par hit, petite pénalité si en-dessous du "cible" mais jamais bloquant
            score += mtf_hits * mtf_bonus_per_hit
            if mtf_hits < mtf_min_hits_target:
                score += mtf_shortfall_penalty

            if _bool(s, "liquidity_ok") or _bool(s, "liquidity_grab_detected"):
                score += liquidity_bonus

            # pénalités douces
            max_spread_pips_soft = _float(entry_rules, "max_spread_pips", 2.0)
            if spread_pips and spread_pips > max_spread_pips_soft:
                score += high_spread_penalty
            if low_vol:
                score += low_vol_penalty

            self.logger.debug(
                "Katana scoring (no-gating) %s -> score=%.4f | conf=%.3f ob=%s fvg=%s m1_break=%s mtf=%d spread=%.2f volZ=%.2f",
                asset, score, confidence, ob, fvg, m1_break, mtf_hits, spread_pips, vol_z
            )

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
            self.logger.info("Aucun actif ne dépasse le seuil minimal de confiance (no-gating).")
            return None

        action = self._infer_action_from_signals(signals.get(best_asset, {}) or {})
        if action is None:
            self.logger.warning("Direction non déterminée pour %s. Trade annulé. (debug=%s)", best_asset, best_debug)
            return None

        self.logger.info(
            "MEILLEUR CANDIDAT KATANA (no-gating): %s | score=%.3f | action=%s | debug=%s",
            best_asset, best_score, action, best_debug
        )

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
        Propose une sortie partielle/totale pour une position ouverte (ou un ajustement de SL) selon des règles Katana.
        - Retourne:
            • {"action":"ADJUST_SL", "asset":..., "new_sl_price":...} OU
            • {"action":"CLOSE", "asset":..., "reason": "..."} OU
            • None si aucune action.
        """
        cfg = self.strategy_config or {}
        exit_cfg = ((cfg.get("exit_rules") or {}).get("scalping") or {})

        asset = str(position.get("symbol") or position.get("asset") or "").upper()
        if not asset:
            return None

        md = (context.get("market_data") or {}).get(asset, {}) or {}
        symbol_info = md.get("symbol_info", {}) or {}
        point  = float(symbol_info.get("point", 0.00001) or 0.00001)
        digits = int(symbol_info.get("digits", 5))

        side = str(position.get("action") or position.get("type") or position.get("side") or "").upper()
        if side not in ("BUY", "SELL"):
            return None

        entry_price = float(position.get("entry_price") or position.get("price_open") or 0.0)
        sl_price    = float(position.get("sl") or 0.0) or None
        tp_price    = float(position.get("tp") or 0.0) or None
        cur_price   = float(md.get("current_price") or position.get("price_current") or 0.0)
        if entry_price <= 0 or cur_price <= 0 or point <= 0:
            return None

        # --- PnL courant en pips (non signé et signé) ---
        pnl_pips_abs = self._pips_between(cur_price, entry_price, point, digits)
        if side == "BUY":
            pnl_pips_signed = (cur_price - entry_price) / (point * (10.0 if digits in (3,5) else 1.0))
        else:
            pnl_pips_signed = (entry_price - cur_price) / (point * (10.0 if digits in (3,5) else 1.0))

        # --- Paramètres de sortie (avec defaults conservateurs) ---
        breakeven_trigger = float(exit_cfg.get("breakeven_trigger_pips", 3.0))
        trailing_start    = float(exit_cfg.get("trailing_start_pips", 5.0))
        trailing_step     = float(exit_cfg.get("trailing_step_pips", 1.0))
        max_hold_seconds  = int(exit_cfg.get("max_hold_seconds", 0))  # 0 = désactivé
        exit_on_m1_flip   = bool(exit_cfg.get("exit_on_m1_phase_flip", True))

        # Derniers signaux/phase pour l’asset
        s = (latest_signals or {}).get(asset, {}) if latest_signals else {}
        phase_m1 = str(s.get("phase_m1") or s.get("phase") or "").lower()

        # --- 1) Break-even auto ---
        if breakeven_trigger > 0 and pnl_pips_signed >= breakeven_trigger:
            # Calcule un SL = entry (ou légèrement positif: +0.1 pip) sans dépasser le prix courant
            be_pad = float(exit_cfg.get("breakeven_pad_pips", 0.1))
            new_sl = entry_price
            if side == "BUY":
                new_sl = min(cur_price, entry_price + be_pad * point * (10.0 if digits in (3,5) else 1.0))
                if not sl_price or new_sl > sl_price:
                    return {"action": "ADJUST_SL", "asset": asset, "new_sl_price": new_sl, "reason": "breakeven"}
            else:
                new_sl = max(cur_price, entry_price - be_pad * point * (10.0 if digits in (3,5) else 1.0))
                if not sl_price or new_sl < sl_price:
                    return {"action": "ADJUST_SL", "asset": asset, "new_sl_price": new_sl, "reason": "breakeven"}

        # --- 2) Trailing léger par pas ---
        if trailing_start > 0 and trailing_step > 0 and pnl_pips_signed >= trailing_start:
            # SL cible = (entrée ± (pnl - step_buffer))
            step_buffer = float(exit_cfg.get("trailing_buffer_pips", trailing_step))
            target_lock = max(0.0, pnl_pips_signed - step_buffer)  # pips à "locker"
            # Convertit en prix
            lock_dist_price = target_lock * point * (10.0 if digits in (3,5) else 1.0)
            new_sl = entry_price + lock_dist_price if side == "BUY" else entry_price - lock_dist_price
            # Ne resserre que si c’est favorable (jamais élargir)
            if side == "BUY":
                if not sl_price or new_sl > sl_price:
                    # borne pour ne pas dépasser le prix courant
                    new_sl = min(new_sl, cur_price)
                    return {"action": "ADJUST_SL", "asset": asset, "new_sl_price": new_sl, "reason": "trail"}
            else:
                if not sl_price or new_sl < sl_price:
                    new_sl = max(new_sl, cur_price)
                    return {"action": "ADJUST_SL", "asset": asset, "new_sl_price": new_sl, "reason": "trail"}

        # --- 3) Flip micro‑phase M1 (optionnel, sortie totale) ---
        if exit_on_m1_flip and phase_m1:
            if side == "BUY" and any(k in phase_m1 for k in ("down", "bear", "expansion_down", "distribution")):
                return {"action": "CLOSE", "asset": asset, "reason": "m1_phase_flip_against"}
            if side == "SELL" and any(k in phase_m1 for k in ("up", "bull", "expansion_up", "accumulation")):
                return {"action": "CLOSE", "asset": asset, "reason": "m1_phase_flip_against"}

        # --- 4) Durée max (optionnel) ---
        if max_hold_seconds and max_hold_seconds > 0:
            # on accepte plusieurs formats d’horodatage en entrée
            import datetime as _dt
            opened_at = position.get("time") or position.get("time_open") or position.get("open_time")
            try:
                if isinstance(opened_at, (int, float)):
                    open_dt = _dt.datetime.utcfromtimestamp(opened_at)
                else:
                    # iso8601 string
                    open_dt = _dt.datetime.fromisoformat(str(opened_at).replace("Z","+00:00")).astimezone(_dt.timezone.utc)
                now_utc = _dt.datetime.fromisoformat(str(context.get("current_time_utc"))).astimezone(_dt.timezone.utc)
                held = (now_utc - open_dt).total_seconds()
                if held >= max_hold_seconds:
                    return {"action": "CLOSE", "asset": asset, "reason": "max_hold_time"}
            except Exception:
                pass

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
