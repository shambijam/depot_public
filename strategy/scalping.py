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
