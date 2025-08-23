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
        self.logger.info(
            "ScalpingStrategy initialisée (sortie par fade du momentum: %s, seuil: %.3f).",
            "ON" if self.enable_momentum_fade_exit else "OFF",
            self.momentum_fade_threshold,
        )

    def _refresh_from_config(self) -> None:
        """Recharge les attributs dérivés de la configuration courante."""
        toggles = self.strategy_config.get("strategy_toggles", {}) or {}
        smart_targets = self.strategy_config.get("smart_targets", {}) or {}

        self.enable_momentum_fade_exit = bool(
            toggles.get("enable_momentum_fade_exit", True)
        )
        # Utiliser float(...) pour éviter les types non numériques
        try:
            self.momentum_fade_threshold = float(
                smart_targets.get("momentum_fade_threshold", 0.1)
            )
        except Exception:
            self.momentum_fade_threshold = 0.1

        self.tradeable_assets: List[str] = list(
            self.strategy_config.get("tradeable_assets", [])
        )
        self.magic_number = self.strategy_config.get("magic_number")
        # Valeurs par défaut raisonnables si absentes
        self.take_profit_pips = float(self.strategy_config.get("take_profit_pips", 10))
        self.stop_loss_pips = float(self.strategy_config.get("stop_loss_pips", 8))

    @staticmethod
    def normalize_spread_points(x: Any) -> Optional[float]:
        """
        Retourne un float `spread_points` valide (> 0) ou None si indisponible/invalid.
        (Utilitaire laissé public car potentiellement réutilisable.)
        """
        try:
            if x is None:
                return None
            x = float(x)
            if not math.isfinite(x) or x <= 0:
                return None
            return x
        except Exception:
            return None

    # ---------------------------------------------------------------------
    # Entrée
    # ---------------------------------------------------------------------
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
        Logique d'entrée Katana (micro-phase, SL/TP serrés).
        - Sélectionne le meilleur actif parmi self.tradeable_assets
        - Exige confluences minimales (OB/FVG, MTF, break M1) selon config
        - Score transparent basé sur confiance, confluences et MTF
        - Ne refait PAS les vérifications de marché (spread, horaires) ici
        """
        self.logger.debug("ScalpingStrategy: évaluation d'entrée (Katana)...")

        # --- Raccourcis config ---
        cfg = self.strategy_config or {}
        entry_rules = ((cfg.get("entry_rules") or {}).get("scalping") or {})
        dec_eng = (cfg.get("decision_engine") or {})
        scoring_cfg = (dec_eng.get("scoring") or {})

        require_mtf_align = bool(entry_rules.get("require_mtf_align", True))
        require_m1_break = bool(entry_rules.get("require_m1_break", True))
        min_conf = float(entry_rules.get("min_confidence", 0.30) or 0.30)

        # poids/bonus par défaut (fallback si absents de la config)
        mtf_bonus_per_hit = float(scoring_cfg.get("mtf_bonus_per_hit", 0.10) or 0.10)
        liquidity_bonus = float(scoring_cfg.get("liquidity_bonus", 0.10) or 0.10)
        ob_conf_bonus = float(scoring_cfg.get("ob_conf_bonus", 0.25) or 0.25)
        base_bias = float(scoring_cfg.get("strategy_bias", 0.30) or 0.30)
        high_spread_penalty = float(scoring_cfg.get("high_spread_penalty", -0.10) or -0.10)
        low_vol_penalty = float(scoring_cfg.get("global_low_vol_penalty", -0.20) or -0.20)

        # --- helpers internes ---
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
            # fallback: bool alignements
            hits = 0
            for k in ("m1_align", "m5_align", "m15_align"):
                if _bool(s, k):
                    hits += 1
            return hits

        # pour info soft (pas de hard block ici)
        spreads = (context.get("spreads_pips") or {})

        best_asset: Optional[str] = None
        best_score: float = float("-inf")
        best_debug: Dict[str, Any] = {}

        for asset in self.tradeable_assets:
            s = signals.get(asset) or {}
            if not s:
                continue

            # 1) Confiance minimale
            confidence = _get_confidence(s)
            if confidence < min_conf:
                self.logger.debug("Asset %s rejeté (confidence %.3f < %.3f).", asset, confidence, min_conf)
                continue

            # 2) Confluences Katana: OB/FVG + break M1 + MTF
            ob = _bool(s, "ob_detected") or _bool(s, "order_block") or _bool(s, "order_block_ml_enhanced")
            fvg = _bool(s, "fvg_detected") or _bool(s, "fvg_enhanced")
            has_confluence = ob or fvg

            if not has_confluence:
                self.logger.debug("Asset %s rejeté (pas de OB/FVG).", asset)
                continue

            if require_m1_break:
                m1_break = _bool(s, "m1_break") or _bool(s, "require_m1_break") or _bool(s, "bos_mss_enhanced")
                if not m1_break:
                    self.logger.debug("Asset %s rejeté (pas de break M1).", asset)
                    continue

            mtf_hits = _get_mtf_hits(s)
            if require_mtf_align and mtf_hits < 1:
                self.logger.debug("Asset %s rejeté (MTF insuffisant: %d).", asset, mtf_hits)
                continue

            # 3) Pénalités soft si infos dispo (pas de hard block ici)
            spread_pips = float(spreads.get(asset, 0.0)) if isinstance(spreads.get(asset), (int, float)) else 0.0
            vol_z = _float(s, "volume_zscore", 0.0)
            low_vol = vol_z < 0.0  # simple heuristique

            # 4) Scoring Katana
            score = 0.0
            score += confidence  # cœur
            score += base_bias
            if ob:
                score += ob_conf_bonus
            if fvg:
                score += ob_conf_bonus * 0.6  # FVG un peu moins pondéré que OB
            score += mtf_hits * mtf_bonus_per_hit
            if _bool(s, "liquidity_ok") or _bool(s, "liquidity_grab_detected"):
                score += liquidity_bonus

            # pénalités douces
            if spread_pips and spread_pips > _float(entry_rules, "max_spread_pips", 2.0):
                score += high_spread_penalty
            if low_vol:
                score += low_vol_penalty

            self.logger.debug(
                "Katana scoring %s -> score=%.4f | conf=%.3f ob=%s fvg=%s mtf=%d spread=%.2f volZ=%.2f",
                asset, score, confidence, ob, fvg, mtf_hits, spread_pips, vol_z
            )

            if score > best_score:
                best_score = score
                best_asset = asset
                best_debug = {
                    "confidence": confidence,
                    "ob": ob,
                    "fvg": fvg,
                    "mtf_hits": mtf_hits,
                    "spread_pips": spread_pips,
                    "volume_zscore": vol_z,
                    "score": score,
                }

        if not best_asset:
            self.logger.info("Aucun actif ne satisfait les exigences Katana (confiance/confluence/MTF).")
            return None

        action = self._infer_action_from_signals(signals.get(best_asset, {}) or {})
        if action is None:
            self.logger.warning("Direction non déterminée pour %s. Trade annulé. (debug=%s)", best_asset, best_debug)
            return None

        self.logger.info("MEILLEUR CANDIDAT KATANA: %s | score=%.3f | action=%s | debug=%s",
                        best_asset, best_score, action, best_debug)

        # Overrides SL/TP serrés (laissez l'exécuteur appliquer les clamps/validations)
        order = {
            "action": action,
            "asset": best_asset,
            "order_type": "MARKET",
            "strategy_type": cfg.get("strategy_name", "scalping"),
            "rule_name": f"scalping:katana:{best_asset}",
            "magic_number": self.magic_number,
            "target_tp_pips": self.take_profit_pips,
            "target_sl_pips": self.stop_loss_pips,
            "comment": "SNIPER_X:scalping_katana",
        }
        return order

 
    def evaluate_exit(
        self, context: Dict[str, Any], current_positions: List[Dict[str, Any]]
    ) -> List[Dict[str, Any]]:
        """
        Sorties Katana (micro-phase) :
        - Breakeven avec lock_points
        - Partial Take Profit
        - Trailing Stop (Chandelier ATR)
        - Momentum fade (fallback/complément)
        ⚠️ Ne touche pas aux vérifications de marché ici.
        """
        self.logger.debug("ScalpingStrategy: évaluation des sorties (Katana)...")

        if not current_positions:
            return []

        exits: List[Dict[str, Any]] = []
        signals_by_asset = context.get("trading_signals", {}) or {}

        # --- lecture config smart targets ---
        cfg = self.strategy_config or {}
        smart_tg = (cfg.get("smart_targets") or {})

        # Breakeven
        be_cfg = (smart_tg.get("breakeven") or {})
        be_enabled = bool(be_cfg.get("enabled", False))
        be_trigger_rr = float(be_cfg.get("trigger_rr", 1.0) or 1.0)
        be_lock_points = float(be_cfg.get("lock_points", 0.0) or 0.0)

        # Partial TP
        pt_cfg = (smart_tg.get("partial_take_profit") or {})
        pt_enabled = bool(pt_cfg.get("enabled", False))
        pt_at_rr = float(pt_cfg.get("at_rr", 1.0) or 1.0)
        pt_close_pct = float(pt_cfg.get("close_percent", 50) or 50.0)

        # Trailing
        tr_cfg = (smart_tg.get("trailing") or {})
        tr_enabled = bool(tr_cfg.get("enabled", False))
        tr_type = str(tr_cfg.get("type", "chandelier")).lower()
        tr_atr_period = int(tr_cfg.get("atr_period", 14) or 14)
        tr_atr_mult = float(tr_cfg.get("atr_multiplier", 1.2) or 1.2)

        # Pour conversion points->prix si fourni dans le contexte
        symbol_meta: Dict[str, Dict[str, Any]] = (context.get("symbol_meta") or {})
        price_map = context.get("current_prices") or context.get("prices") or {}

        for pos in current_positions:
            # filtre stratégie
            if pos.get("magic") != self.magic_number:
                continue

            asset = pos.get("symbol")
            if not asset:
                continue

            is_buy = (pos.get("type") == 0)  # 0=BUY, 1=SELL dans MT5
            entry = float(pos.get("price_open") or pos.get("price") or 0.0)
            cur_price = float(
                (price_map.get(asset) or pos.get("price_current") or 0.0)
            )
            sl = float(pos.get("sl") or 0.0)
            tp = float(pos.get("tp") or 0.0)
            ticket = pos.get("ticket")

            if entry <= 0.0 or cur_price <= 0.0:
                # Pas de data fiable -> on ne décide pas
                continue

            # distances de risque (en prix)
            risk_dist = (entry - sl) if is_buy else (sl - entry)
            reward_prog = (cur_price - entry) if is_buy else (entry - cur_price)
            # RR courant (peut être négatif/0 en début)
            rr_now = reward_prog / risk_dist if (risk_dist and risk_dist > 0) else 0.0

            # Meta pour conversion points->prix (fallback: 0 -> lock à BE exact)
            meta = symbol_meta.get(asset) or {}
            point = float(meta.get("point", 0.0) or 0.0)
            digits = int(meta.get("digits", 0) or 0)

            # ------- 1) Breakeven avec lock_points -------
            if be_enabled and rr_now >= be_trigger_rr:
                # SL à l’entrée + petit lock (en points si 'point' disponible)
                lock_price = be_lock_points * point if point > 0 else 0.0
                new_sl = (entry + lock_price) if is_buy else (entry - lock_price)
                # Ne jamais réduire la protection
                if (is_buy and (sl <= 0 or new_sl > sl)) or (not is_buy and (sl <= 0 or new_sl < sl)):
                    if digits:
                        new_sl = round(new_sl, digits)
                    exits.append(
                        {
                            "ticket_to_modify": ticket,
                            "new_sl_price": new_sl,
                            "reason": "breakeven_lock",
                            "asset": asset,
                        }
                    )
                    # met à jour le sl local pour les étapes suivantes
                    sl = new_sl
                    risk_dist = (entry - sl) if is_buy else (sl - entry)
                    rr_now = reward_prog / risk_dist if (risk_dist and risk_dist > 0) else rr_now

            # ------- 2) Partial Take Profit -------
            # On émet une instruction PARTIAL; l'exécuteur doit gérer l'idempotence/flag.
            if pt_enabled and rr_now >= pt_at_rr and 0 < pt_close_pct < 100:
                exits.append(
                    {
                        "ticket_to_partial_close": ticket,
                        "close_percent": pt_close_pct,
                        "reason": "partial_tp_rr",
                        "asset": asset,
                    }
                )

            # ------- 3) Trailing Stop (Chandelier ATR) -------
            # ATR récupéré depuis les signaux/ marché si possible
            if tr_enabled and tr_type == "chandelier":
                # essayer d'obtenir un ATR (ex: 'atr14') depuis les signaux
                sig = signals_by_asset.get(asset) or {}
                atr_val = None
                for k in ("atr", "atr14", f"atr{tr_atr_period}"):
                    v = sig.get(k)
                    if isinstance(v, (int, float)) and v > 0:
                        atr_val = float(v)
                        break
                # Si ATR dispo, proposer un trailing
                if isinstance(atr_val, float) and atr_val > 0:
                    trail_dist = tr_atr_mult * atr_val
                    new_sl = (cur_price - trail_dist) if is_buy else (cur_price + trail_dist)

                    # ne jamais "desserrer" le SL
                    tighten = (is_buy and (sl <= 0 or new_sl > sl)) or (not is_buy and (sl <= 0 or new_sl < sl))
                    # éviter SL au-delà de l'entrée si déjà au BE (tolère micro backfills)
                    if tighten:
                        if digits:
                            new_sl = round(new_sl, digits)
                        exits.append(
                            {
                                "ticket_to_modify": ticket,
                                "new_sl_price": new_sl,
                                "reason": "trail_chandelier_atr",
                                "asset": asset,
                            }
                        )
                        sl = new_sl  # mise à jour locale

            # ------- 4) Momentum fade (complément / fallback) -------
            if self.enable_momentum_fade_exit:
                asset_signals = signals_by_asset.get(asset) or {}
                try:
                    vol_mom = float(asset_signals.get("volume_momentum", 0.0))
                except Exception:
                    vol_mom = 0.0

                # BUY: si momentum <= +threshold  => sortie
                # SELL: si momentum >= -threshold => sortie
                should_exit = (is_buy and vol_mom <= self.momentum_fade_threshold) or (
                    not is_buy and vol_mom >= -self.momentum_fade_threshold
                )
                if should_exit:
                    self.logger.info(
                        "EXIT (momentum fade): ticket=%s | %s | vol_mom=%.3f | seuil=±%.3f",
                        ticket,
                        asset,
                        vol_mom,
                        self.momentum_fade_threshold,
                    )
                    exits.append(
                        {
                            "ticket_to_close": ticket,
                            "reason": "momentum_fade",
                            "asset": asset,
                        }
                    )

        return exits
   
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
