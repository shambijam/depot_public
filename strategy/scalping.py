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
        Logique d'entrée : faire confiance aux signaux du DecisionPipeline et
        sélectionner le meilleur actif pour le scalping.
        On ne refait PAS les vérifications de marché ici.
        """
        self.logger.debug(
            "ScalpingStrategy: sélection du meilleur candidat d'entrée..."
        )

        best_asset: Optional[str] = None
        best_score: float = float("-inf")

        # Itère uniquement sur les actifs déclarés « tradables » dans la config
        for asset in self.tradeable_assets:
            asset_signals = signals.get(asset)
            if not asset_signals:
                continue

            # Score simple: confiance + |momentum_volume|
            try:
                confidence = float(asset_signals.get("confidence_score", 0.0))
            except Exception:
                confidence = 0.0
            try:
                volume_momentum = float(asset_signals.get("volume_momentum", 0.0))
            except Exception:
                volume_momentum = 0.0

            current_score = confidence + abs(volume_momentum)

            if current_score > best_score:
                best_score = current_score
                best_asset = asset

        if not best_asset:
            self.logger.info(
                "Aucun signal d'actif jugé suffisant pour une entrée de scalping."
            )
            return None

        final_signals = signals.get(best_asset, {})
        action = self._infer_action_from_signals(final_signals)

        if action is None:
            self.logger.warning(
                "Direction non déterminée pour %s. Trade annulé. (signals=%s)",
                best_asset,
                {
                    k: final_signals.get(k)
                    for k in [
                        "phase",
                        "direction",
                        "trend",
                        "volume_momentum",
                        "action",
                    ]
                },
            )
            return None

        self.logger.info(
            "MEILLEUR CANDIDAT: %s | score=%.3f | action=%s",
            best_asset,
            best_score,
            action,
        )

        order = {
            "action": action,
            "asset": best_asset,
            "order_type": "MARKET",
            "strategy_type": self.strategy_config.get("strategy_name", "scalping"),
            "rule_name": f"scalping:auto:{best_asset}",
            "magic_number": self.magic_number,
            "target_tp_pips": self.take_profit_pips,
            "target_sl_pips": self.stop_loss_pips,
            # Champs facultatifs si utilisés par l'exécuteur d'ordres
            "comment": "SNIPER_X:scalping",
        }
        return order

    # ---------------------------------------------------------------------
    # Sortie
    # ---------------------------------------------------------------------
    def evaluate_exit(
        self, context: Dict[str, Any], current_positions: List[Dict[str, Any]]
    ) -> List[Dict[str, Any]]:
        """
        Évalue les conditions de sortie pour le scalping, principalement basées sur
        l'affaiblissement du momentum qui a initié le trade.
        """
        self.logger.debug("ScalpingStrategy: évaluation des sorties...")
        if not self.enable_momentum_fade_exit or not current_positions:
            return []

        exit_decisions: List[Dict[str, Any]] = []
        signals_by_asset = context.get("trading_signals", {}) or {}

        for position in current_positions:
            # Filtrer uniquement les positions ouvertes par cette stratégie
            if position.get("magic") != self.magic_number:
                continue

            asset = position.get("symbol")
            if not asset:
                continue

            asset_signals = signals_by_asset.get(asset) or {}
            try:
                vol_mom = float(asset_signals.get("volume_momentum", 0.0))
            except Exception:
                vol_mom = 0.0

            is_buy = position.get("type") == 0  # 0 = BUY dans MT5

            # BUY: si momentum <= +threshold  => sortie
            # SELL: si momentum >= -threshold => sortie
            should_exit = (is_buy and vol_mom <= self.momentum_fade_threshold) or (
                not is_buy and vol_mom >= -self.momentum_fade_threshold
            )

            if should_exit:
                self.logger.info(
                    "EXIT (momentum fade): ticket=%s | %s | vol_mom=%.3f | seuil=±%.3f",
                    position.get("ticket"),
                    asset,
                    vol_mom,
                    self.momentum_fade_threshold,
                )
                exit_decisions.append(
                    {
                        "ticket_to_close": position.get("ticket"),
                        "reason": "momentum_fade",
                        "asset": asset,
                    }
                )

        return exit_decisions

    # ---------------------------------------------------------------------
    # Paramètres
    # ---------------------------------------------------------------------
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
