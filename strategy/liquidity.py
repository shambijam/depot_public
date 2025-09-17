# strategy/liquidity.py

from typing import Dict, Any, List, Optional, Tuple
from .base_strategy import BaseStrategy
import math
import pandas as pd
import numpy as np


class LiquidityStrategy(BaseStrategy):
    """
    Stratégie 'Liquidity' — institutionnelle
    - Switch automatique quand une zone de liquidité est identifiée
    - Entrée selon entry_logic (break_of_absorption_extreme ou retracement OB/FVG)
    - SL derrière l'extrême du sweep (fallback ATR)
    - TP vers la prochaine poche de liquidité (EQH/EQL > OB > FVG ; fallback ATR)
    - Respecte decision_engine.execution (order_type = limit, mitigation, etc.)
    """

    # =========================
    #         LIFECYCLE
    # =========================
    def __init__(self, config_manager_instance: Any, strategy_config: Dict[str, Any]):
        super().__init__(config_manager_instance, strategy_config)
        self.logger.info("Moteur de stratégie Liquidity initialisé.")

    # =========================
    #      PUBLIC METHODS
    # =========================
    def evaluate_entry(
        self, context: Dict[str, Any], signals: Dict[str, Any]
    ) -> Optional[Dict[str, Any]]:
        """
        Décide d'une entrée Liquidity par actif, puis sélectionne le meilleur.
        Utilise les signaux: sweep_detected, absorption_confirmed, bos_mss_detected,
        fvg_details, ob_details, eqh_eql_details, confidence_score.
        """
        tradeable_assets = self._cfg_list("tradeable_assets", default=[])
        if not tradeable_assets:
            self.logger.warning("[LIQ] Aucun asset tradable configuré.")
            return None
        
            # Vérification stricte: ignorer les actifs hors whitelist
            invalid_assets = [a for a in signals.keys() if a not in tradeable_assets]
            if invalid_assets:
                self.logger.info(f"[LIQ] Ignorés (non autorisés): {invalid_assets} (whitelist={tradeable_assets})")


        min_conf = float(self.strategy_config.get("min_confidence_for_entry", 0.6))
        best: Tuple[str, float, Dict[str, Any]] = ("", min_conf, {})

        for asset in tradeable_assets:
            sig = signals.get(asset) or {}
            if not sig:
                continue

            # Conditions coeur Liquidity
            sweep = bool(sig.get("sweep_detected", False))
            absorb = bool(sig.get("absorption_confirmed", False))
            bos_ok = bool(sig.get("bos_mss_detected", False))  # impulsion/validation
            switch_flag = bool(sig.get("switch_to_liquidity", False))

            if not (sweep or absorb or switch_flag or bos_ok):
                continue  # pas de setup liquidity

            confidence = float(sig.get("confidence_score", 0.0) or 0.0)
            if confidence < min_conf:
                # on reste strict mais paramétrable par config
                continue

            # Construire une proposition d'ordre pour cet asset
            try:
                proposal = self._build_order_proposal(asset, context, sig)
            except Exception as e:
                self.logger.warning(
                    f"[LIQ] Impossible de construire une proposition pour {asset}: {e}"
                )
                continue

            if not proposal:
                continue

            # Scorer la proposition (simple: on priorise la confiance, puis la distance TP/SL)
            prop_score = confidence + 0.01 * proposal.get("rr_estimate", 0.0)
            if prop_score > best[1]:
                best = (asset, prop_score, proposal)

        if not best[0]:
            self.logger.info(
                "[LIQ] Aucun actif ne dépasse le seuil de confiance/liquidity."
            )
            return None

        asset, _, proposal = best

        # === LOGGING DÉTAILLÉ LIQUIDITY ===
        side = proposal.get("side")
        entry = proposal.get("entry_price")
        sl = proposal.get("sl_price")
        tp_list = proposal.get("tp_price")
        rr_est = proposal.get("rr_estimate")
        conf = proposal.get("confidence", 0.0)

        self.logger.info(
            f"[LIQUIDITY] 🔍 {asset} | Side={side} | "
            f"Entry={entry:.5f} | SL={sl:.5f} | TPs={tp_list} | "
            f"RR={rr_est:.2f} | Confiance={conf:.2f}"
        )

       # Package final pour l’executor
        decision = self._build_decision_package_from_proposal(asset, proposal) or {}
        decision["strategy_type"] = "liquidity"  # ✅ ajouté pour audit & logs
        decision.setdefault("rule_name", "liquidity_entry")  # fallback propre si absent
        return decision


    def _apply_break_even(self, pos: dict, context: dict, rr_threshold: float = 1.0):
        """
        Déplace le SL au prix d'entrée dès que le prix atteint le RR cible.
        rr_threshold = 1.0 => break-even à 1R
        """
        entry = float(pos.get("entry_price") or 0)
        sl = float(pos.get("sl_price") or 0)
        tp = float(pos.get("tp_price") or 0)
        last_price = float(
            context.get("trading_signals", {}).get(pos["symbol"], {}).get("close", 0)
        )

        if not (entry and sl and tp and last_price):
            return None

        # Calcul RR actuel
        risk = abs(entry - sl)
        reward = (
            abs(last_price - entry) if pos.get("type") == 0 else abs(entry - last_price)
        )
        rr_now = reward / risk if risk > 0 else 0

        if rr_now >= rr_threshold and sl != entry:
            return {
                "ticket_to_update": pos["ticket"],
                "new_sl": entry,
                "reason": f"Break-even atteint ({rr_now:.2f}R)",
            }
        return None

    def _apply_trailing_stop(
        self, pos: dict, context: dict, rr_start: float = 1.5, atr_mult: float = 1.2
    ):
        """
        Trailing Stop Liquidity :
        - Active au-delà d'un RR cible (par défaut 1.5R)
        - Place le SL dynamique via ATR (ex: 1.2 * ATR)
        """

        entry = float(pos.get("entry_price") or 0)
        sl = float(pos.get("sl_price") or 0)
        last_price = float(
            context.get("trading_signals", {}).get(pos["symbol"], {}).get("close", 0)
        )

        if not (entry and sl and last_price):
            return None

        # RR actuel
        risk = abs(entry - sl)
        reward = (
            abs(last_price - entry) if pos.get("type") == 0 else abs(entry - last_price)
        )
        rr_now = reward / risk if risk > 0 else 0

        if rr_now < rr_start:
            return None  # trailing pas encore activé

        # Données marché pour ATR
        md = (context.get("market_data") or {}).get(pos["symbol"])
        rates_df = md.get("rates_df") if isinstance(md, dict) else None
        if not isinstance(rates_df, pd.DataFrame) or rates_df.empty:
            return None

        # ATR simple
        high, low, close = rates_df["high"], rates_df["low"], rates_df["close"]
        tr = np.maximum.reduce(
            [
                (high - low).abs(),
                (high - close.shift(1)).abs(),
                (low - close.shift(1)).abs(),
            ]
        )
        atr = tr.rolling(window=14, min_periods=14).mean().iloc[-1]

        if not atr or atr <= 0:
            return None

        # Nouveau SL trailing
        if pos.get("type") == 0:  # BUY
            new_sl = last_price - atr_mult * atr
            if new_sl > sl:
                return {
                    "ticket_to_update": pos["ticket"],
                    "new_sl": new_sl,
                    "reason": "Trailing Stop ATR",
                }
        else:  # SELL
            new_sl = last_price + atr_mult * atr
            if new_sl < sl:
                return {
                    "ticket_to_update": pos["ticket"],
                    "new_sl": new_sl,
                    "reason": "Trailing Stop ATR",
                }

        return None

    def evaluate_exit(
        self, context: Dict[str, Any], current_positions: List[Dict[str, Any]]
    ) -> List[Dict[str, Any]]:
        """
        Évalue les sorties Liquidity (institutionnelles) :
        1. Exit Invalidation : prix retourne dans la zone sweep
        2. Exit Volume : spike de volume à contre-sens
        3. Exit Temps : position ouverte trop longtemps
        4. Exit Rules classiques de la config
        """
        exit_decisions: List[Dict[str, Any]] = []
        cfg_rules = self.strategy_config.get("exit_rules", {})
        liqui_exit_cfg = self.strategy_config.get("liquidity_exit", {})

        # Paramètres depuis la config
        max_bars = int(liqui_exit_cfg.get("max_bars_in_trade", 0) or 0)
        vol_exit_threshold = float(liqui_exit_cfg.get("volume_exit_threshold", 3.0))

        for pos in current_positions:
            if pos.get("magic") != self.strategy_config.get("magic_number"):
                continue

            asset = pos.get("symbol")
            asset_sig = context.get("trading_signals", {}).get(asset) or {}
            if not asset_sig:
                continue

            side = "BUY" if pos.get("type") == 0 else "SELL"  # 0=BUY,1=SELL (MT5 conv.)
            last_price = float(asset_sig.get("close", 0.0) or 0.0)

            # === 1) Exit Invalidation : retour dans sweep ===
            sweep_extreme = None
            sd = asset_sig.get("sweep_details")
            if isinstance(sd, dict) and "extreme_price" in sd:
                sweep_extreme = float(sd.get("extreme_price") or 0.0)
            elif isinstance(sd, list) and sd:
                for x in reversed(sd):
                    if (
                        isinstance(x, dict)
                        and x.get("present")
                        and x.get("extreme_price")
                    ):
                        sweep_extreme = float(x["extreme_price"])
                        break

            if sweep_extreme:
                if side == "BUY" and last_price < sweep_extreme:
                    self.logger.info(
                        f"[LIQUIDITY] ⛔ Exit {asset} (BUY) : invalidation sweep (price={last_price:.5f} < sweep={sweep_extreme:.5f})"
                    )
                    exit_decisions.append(
                    {
                        "ticket_to_close": pos["ticket"],
                        "reason": "Invalidation Sweep",
                        "strategy_type": "liquidity",      
                        "rule_name": "exit_invalidation", 
                    }
                )

                    continue
                if side == "SELL" and last_price > sweep_extreme:
                    self.logger.info(
                        f"[LIQUIDITY] ⛔ Exit {asset} (SELL) : invalidation sweep (price={last_price:.5f} > sweep={sweep_extreme:.5f})"
                    )
                    exit_decisions.append(
                    {
                        "ticket_to_close": pos["ticket"],
                        "reason": "Invalidation Sweep",
                        "strategy_type": "liquidity",      
                        "rule_name": "exit_invalidation", 
                    }
                )

                    continue

            # === 2) Exit Volume : spike anormal contre la position ===
            vol_z = float(asset_sig.get("volume_zscore", 0.0) or 0.0)
            if vol_exit_threshold > 0 and vol_z >= vol_exit_threshold:
                regime = str(asset_sig.get("regime", "")).lower()
                if side == "BUY" and "bear" in regime:
                    self.logger.info(
                        f"[LIQUIDITY] ⛔ Exit {asset} (BUY) : volume spike adverse (vol_z={vol_z:.2f}, regime={regime})"
                    )
                    exit_decisions.append(
                        {
                            "ticket_to_close": pos["ticket"],
                            "reason": "Volume spike adverse",
                        }
                    )
                    continue
                if side == "SELL" and "bull" in regime:
                    self.logger.info(
                        f"[LIQUIDITY] ⛔ Exit {asset} (SELL) : volume spike adverse (vol_z={vol_z:.2f}, regime={regime})"
                    )
                    exit_decisions.append(
                        {
                            "ticket_to_close": pos["ticket"],
                            "reason": "Volume spike adverse",
                        }
                    )
                    continue

                # === 2bis) Trailing Stop institutionnel ===
            ts_decision = self._apply_trailing_stop(
                pos, context, rr_start=1.5, atr_mult=1.2
            )
            if ts_decision:
                exit_decisions.append(ts_decision)
                continue

            # === 3) Exit Temps : position ouverte trop longtemps ===
            if max_bars > 0:
                from datetime import datetime

                try:
                    opened_at = pos.get("open_time")
                    if opened_at:
                        open_dt = datetime.fromisoformat(str(opened_at))
                        now = datetime.utcnow()
                        elapsed_minutes = (now - open_dt).total_seconds() / 60.0
                        bar_size_min = int(
                            self.config_manager.get("execution.bar_size_minutes", 1)
                        )
                        bars_elapsed = int(elapsed_minutes // bar_size_min)
                        if bars_elapsed >= max_bars:
                            self.logger.info(
                                f"[LIQUIDITY] ⏱ Exit {asset} : max bars atteint ({bars_elapsed} >= {max_bars})"
                            )
                            exit_decisions.append(
                                {
                                    "ticket_to_close": pos["ticket"],
                                    "reason": "Max bars in trade atteint",
                                }
                            )
                            continue
                except Exception as e:
                    self.logger.warning(f"[EXIT] Erreur calcul max_bars: {e}")

            # === 4) Exit Rules de la config ===
            if isinstance(cfg_rules, list):
                for rule in cfg_rules:
                    if self._check_rule_conditions(
                        asset_sig, rule.get("conditions", {})
                    ):
                        self.logger.info(
                            f"[LIQUIDITY] ⛔ Exit {asset} par règle config: {rule.get('name', 'Exit Liquidity')}"
                        )
                        exit_decisions.append(
                            {
                                "ticket_to_close": pos["ticket"],
                                "reason": rule.get("name", "Exit Liquidity"),
                            }
                        )
                        break
            elif isinstance(cfg_rules, dict):
                for name, rule in cfg_rules.items():
                    if self._check_rule_conditions(
                        asset_sig, rule.get("conditions", {})
                    ):
                        self.logger.info(
                            f"[LIQUIDITY] ⛔ Exit {asset} par règle config: {name}"
                        )
                        exit_decisions.append(
                            {"ticket_to_close": pos["ticket"], "reason": name}
                        )
                        break

                # === 5) Exit Trailing Stop Liquidity ===
            trail_cfg = liqui_exit_cfg.get("trailing_stop", {})
            if trail_cfg.get("enabled", False):
                atr_period = int(trail_cfg.get("atr_period", 14))
                atr_mult = float(trail_cfg.get("atr_multiplier", 2.0))

                md = (context.get("market_data") or {}).get(asset, {})
                df = md.get("rates_df")
                if isinstance(df, pd.DataFrame) and len(df) >= atr_period + 2:
                    high, low, close = df["high"], df["low"], df["close"]
                    tr = np.maximum.reduce(
                        [
                            (high - low).abs(),
                            (high - close.shift(1)).abs(),
                            (low - close.shift(1)).abs(),
                        ]
                    )
                    atr = tr.rolling(window=atr_period).mean().iloc[-1]

                    if atr and atr > 0:
                        if side == "BUY":
                            new_sl = last_price - atr_mult * atr
                            if new_sl > pos.get("sl", 0):  # seulement si SL monte
                                self.logger.info(
                                    f"[LIQUIDITY] 🔄 Trailing SL BUY {asset}: {pos.get('sl')} -> {new_sl:.5f}"
                                )
                                exit_decisions.append(
                                    {
                                        "ticket_to_close": pos["ticket"],
                                        "reason": "Trailing Stop Update",
                                        "new_sl": new_sl,
                                    }
                                )
                        elif side == "SELL":
                            new_sl = last_price + atr_mult * atr
                            if new_sl < pos.get(
                                "sl", float("inf")
                            ):  # seulement si SL descend
                                self.logger.info(
                                    f"[LIQUIDITY] 🔄 Trailing SL SELL {asset}: {pos.get('sl')} -> {new_sl:.5f}"
                                )
                                exit_decisions.append(
                                    {
                                        "ticket_to_close": pos["ticket"],
                                        "reason": "Trailing Stop Update",
                                        "new_sl": new_sl,
                                    }
                                )
            return exit_decisions

    def get_parameters(self) -> Dict[str, Any]:
        return self.strategy_config.copy()

    def update_strategy_parameters(self, new_params: Dict[str, Any]) -> None:
        self.logger.info(f"Mise à jour des paramètres Liquidity: {new_params}")
        self.strategy_config.update(new_params)
        self.__init__(self.config_manager, self.strategy_config)

    # =========================
    #     INTERNAL HELPERS
    # =========================
    def _build_order_proposal(
        self, asset: str, context: Dict[str, Any], sig: Dict[str, Any]
    ) -> Optional[Dict[str, Any]]:
        """
        Construit la proposition d'ordre:
        - déduire le sens (opposé au sweep, sinon via phase/bos)
        - calculer entry selon entry_logic (break vs retracement)
        - calculer SL (derrière sweep ou fallback ATR)
        - calculer TP (EQH/EQL > OB > FVG ; fallback ATR)
        - calculer RR estimé
        """
        # --- paramètres / marché ---
        point = float(sig.get("point", context.get("point", 0.00001)) or 0.00001)
        pip_size = point * 10.0 if point > 0 else 0.0001
        price = float(
            sig.get("close", context.get("close", 0.0)) or 0.0
        )  # dernier close si dispo

        # --- entry_logic & execution ---
        entry_logic = self._cfg_dict("liquidity_sweep.entry_logic", default={})
        trigger = str(entry_logic.get("trigger", "break_of_absorption_extreme")).lower()
        buffer_pips = float(entry_logic.get("buffer_pips", 0.4))
        timeout_bars = int(entry_logic.get("timeout_bars", 3))
        direction_pref = str(entry_logic.get("direction", "opposite_of_sweep")).lower()

        exec_cfg = self._cfg_dict("decision_engine.execution", default={})
        order_type = str(exec_cfg.get("order_type", "limit")).upper()
        use_mitigation = bool(exec_cfg.get("limit_use_mitigation", True))

        # --- sens du trade ---
        side = self._infer_direction(sig, direction_pref)
        if side not in ("BUY", "SELL"):
            return None

        # --- niveaux 'sweep' / 'absorption' ---
        sweep_extreme, absorb_extreme = self._extract_sweep_absorption_extremes(
            sig, side
        )

        # --- entry price ---
        entry_price = self._compute_entry_price(
            side=side,
            trigger=trigger,
            buffer_pips=buffer_pips,
            pip_size=pip_size,
            price_now=price,
            sweep_extreme=sweep_extreme,
            absorb_extreme=absorb_extreme,
            sig=sig,
            use_mitigation=use_mitigation,
        )
        if entry_price is None or entry_price <= 0:
            return None

        # --- SL ---
        sl_price = self._compute_sl(
            side=side,
            pip_size=pip_size,
            sweep_extreme=sweep_extreme,
            sig=sig,
        )
        if sl_price is None or sl_price <= 0:
            return None

        # --- TP ---
        tp_price = self._compute_tp(
            asset=asset,
            side=side,
            entry_price=entry_price,
            pip_size=pip_size,
            sig=sig,
        )

        # --- RR estimé ---
        rr_est = self._estimate_rr(entry_price, sl_price, tp_price, side)

        proposal = {
            "action": side,
            "entry_price": entry_price,
            "sl_price": sl_price,
            "tp_price": tp_price,
            "order_type": order_type,  # resp. config: LIMIT attendu pour Liquidity
            "buffer_pips": buffer_pips,
            "timeout_bars": timeout_bars,
            "rr_estimate": rr_est,
            "use_mitigation": use_mitigation,
            # Champs ajoutés pour compatibilité et audit
            "confidence": float(sig.get("confidence_score", 0.0)),  # ✅ score du signal
            "rule_name": "liquidity_sweep_absorption",              # ✅ identifiant clair
        }
        return proposal


    def _infer_direction(self, sig: Dict[str, Any]) -> Optional[str]:
        """
        Détermine la direction (BUY/SELL) d'un setup Liquidity.
        Priorité :
        1. Sweep (low→BUY, high→SELL)
        2. Validation MTF bias
        3. Phase/Regime si sweep absent
        ❌ Aucun fallback : si incertain → None
        """
        side = None

        # === 1) Sweep dominant ===
        sd = sig.get("sweep_details")
        if isinstance(sd, dict) and sd.get("present"):
            if sd.get("sweep_type") == "low":
                side = "BUY"
            elif sd.get("sweep_type") == "high":
                side = "SELL"
        elif isinstance(sd, list) and sd:
            for x in reversed(sd):
                if isinstance(x, dict) and x.get("present"):
                    if x.get("sweep_type") == "low":
                        side = "BUY"
                    elif x.get("sweep_type") == "high":
                        side = "SELL"
                    break

        # === 2) Phase/Regime comme backup interne (toujours Liquidity, pas fallback externe) ===
        if not side:
            phase = str(sig.get("phase", "")).lower()
            regime = str(sig.get("regime", "")).lower()
            if "impulse" in phase or "impulsion" in regime:
                side = "BUY" if "bull" in regime else "SELL"

        # === 3) Validation MTF bias ===
        mtf_bias = sig.get("mtf_bias")
        mtf_aligned = sig.get("mtf_bias_aligned", True)

        if not mtf_aligned:
            self.logger.warning(
                "[LIQUIDITY] ❌ Direction rejetée : MTF bias non aligné."
            )
            return None

        if mtf_bias:
            mtf_bias = str(mtf_bias).upper()
            if side and side != mtf_bias:
                self.logger.warning(
                    f"[LIQUIDITY] ❌ Conflit directionnel: side={side}, mtf_bias={mtf_bias}"
                )
                return None
            side = mtf_bias if not side else side

        if not side:
            self.logger.warning(
                "[LIQUIDITY] ❌ Direction indécise (aucun sweep/phase/MTF)."
            )
            return None

        self.logger.info(f"[LIQUIDITY] ✅ Direction confirmée: {side}")
        return side

    def _last_sweep_side(self, sig: Dict[str, Any]) -> Optional[str]:
        """
        Retourne 'buy' (sweep down) ou 'sell' (sweep up) si disponible dans sweep_details.
        sweep_details peut être:
        - une liste alignée sur les barres (dict ou None)
        - un dict 'last' avec 'side'
        """
        sd = sig.get("sweep_details")
        if isinstance(sd, dict) and "side" in sd:
            # format compact
            return str(sd.get("side")).lower()
        if isinstance(sd, list) and len(sd) > 0:
            # chercher le dernier dict non None
            for x in reversed(sd):
                if isinstance(x, dict) and x.get("side"):
                    return str(x.get("side")).lower()
        return None

    def _extract_sweep_absorption_extremes(
        self, sig: Dict[str, Any], side: str
    ) -> Tuple[Optional[float], Optional[float]]:
        """
        Déduit les extrêmes utiles :
        - sweep_extreme: le HH/LL sweepé (référence SL)
        - absorb_extreme: l'extrême de la/les bougies d'absorption (référence pour break entry)
        """
        sweep_extreme = None
        sd = sig.get("sweep_details")
        if isinstance(sd, dict):
            sweep_extreme = float(sd.get("extreme_price", 0.0) or 0.0) or None
        elif isinstance(sd, list) and len(sd) > 0:
            for x in reversed(sd):
                if isinstance(x, dict) and x.get("present"):
                    # pas toujours fourni; on tente 'extreme_price' sinon None
                    val = x.get("extreme_price")
                    if val is not None:
                        sweep_extreme = float(val)
                        break

        absorb_extreme = None
        ad = sig.get("absorption_details")
        if isinstance(ad, dict):
            absorb_extreme = float(ad.get("extreme_price", 0.0) or 0.0) or None
        elif isinstance(ad, list) and len(ad) > 0:
            for x in reversed(ad):
                if isinstance(x, dict) and x.get("confirmed"):
                    val_h = x.get("high")
                    val_l = x.get("low")
                    if side == "BUY" and val_h is not None:
                        absorb_extreme = float(val_h)
                        break
                    if side == "SELL" and val_l is not None:
                        absorb_extreme = float(val_l)
                        break

        return sweep_extreme, absorb_extreme

    def _compute_entry_price(
        self,
        side: str,
        trigger: str,
        buffer_pips: float,
        pip_size: float,
        price_now: float,
        sweep_extreme: Optional[float],
        absorb_extreme: Optional[float],
        sig: Dict[str, Any],
        use_mitigation: bool,
    ) -> Optional[float]:
        """
        ENTRY LOGIC (dev desk):
        - break_of_absorption_extreme ± buffer
        - retracement priorisant zone OB∩FVG (confluence), sinon OB puis FVG
        - wick_fill: entrée à X% du remplissage de la mèche du sweep
        - support MTF si des zones HTF sont exposées dans les signaux
        - sélection du niveau le plus proche dans le bon sens (avec tolérance)
        """

        # --- Config lecture (avec défauts sûrs) ---
        el_cfg = self._cfg_dict("liquidity_sweep.entry_logic", {})
        prefer_confluent = bool(el_cfg.get("prefer_confluent_zone", True))
        use_wick_fill = bool(el_cfg.get("use_wick_fill", False))
        wick_fill_ratio = float(el_cfg.get("wick_fill_ratio", 0.50))  # 0..1
        use_mtf_zone = bool(el_cfg.get("use_mtf_zone", False))
        retr_tolerance_pips = float(el_cfg.get("retracement_tolerance_pips", 3.0))
        retr_tolerance_px = max(0.0, retr_tolerance_pips) * pip_size

        # Buffer en prix
        buf = float(buffer_pips) * pip_size

        # Helpers locaux pour lire une "zone"
        def zone_band_from_dict(d: Dict[str, Any]) -> Optional[Tuple[float, float]]:
            if not isinstance(d, dict):
                return None
            keys_hi = ("zone_high", "upper", "high", "hi")
            keys_lo = ("zone_low", "lower", "low", "lo")
            z_hi = None
            z_lo = None
            for k in keys_hi:
                if k in d and d[k] is not None:
                    z_hi = float(d[k])
                    break
            for k in keys_lo:
                if k in d and d[k] is not None:
                    z_lo = float(d[k])
                    break
            if z_hi is None and z_lo is not None:
                z_hi = z_lo
            if z_lo is None and z_hi is not None:
                z_lo = z_hi
            if z_lo is None or z_hi is None:
                return None
            lo, hi = (min(z_lo, z_hi), max(z_lo, z_hi))
            return (lo, hi)

        def zone_band_from_obj(obj: Any) -> Optional[Tuple[float, float]]:
            if isinstance(obj, dict):
                return zone_band_from_dict(obj)
            if isinstance(obj, list) and obj:
                # On prend la plus récente zone valide (en partant de la fin)
                for x in reversed(obj):
                    zb = zone_band_from_dict(x) if isinstance(x, dict) else None
                    if zb:
                        return zb
            return None

        def zone_overlap(
            a: Tuple[float, float], b: Tuple[float, float]
        ) -> Optional[Tuple[float, float]]:
            lo = max(a[0], b[0])
            hi = min(a[1], b[1])
            return (lo, hi) if hi >= lo else None

        def side_entry_edge(band: Tuple[float, float], side_: str) -> float:
            lo, hi = band
            return lo if side_ == "BUY" else hi  # BUY: bas de zone, SELL: haut de zone

        def nearest_valid_retracement(
            side_: str, candidates: List[float], ref_price: float
        ) -> Optional[float]:
            # Filtre directionnel + plus proche du prix actuel
            # BUY: on veut une limite EN-DESSOUS du prix actuel; SELL: EN-DESSUS
            if side_ == "BUY":
                cands = [c for c in candidates if c <= ref_price + retr_tolerance_px]
                if not cands:
                    return None
                # plus proche par dessous (ou très légèrement dessus si toléré)
                cands.sort(key=lambda x: (abs(ref_price - x), -x))
                return cands[0]
            else:
                cands = [c for c in candidates if c >= ref_price - retr_tolerance_px]
                if not cands:
                    return None
                # plus proche par dessus
                cands.sort(key=lambda x: (abs(x - ref_price), x))
                return cands[0]

        # --- 0) Wick Fill (optionnel) ---
        # Si activé, on peut forcer une entrée à X% du remplissage de la mèche
        # entre l'extrême du sweep et l'extrême d'absorption.
        # BUY : entrée = sweep_low + ratio*(absorb_extreme - sweep_low)
        # SELL: entrée = sweep_high - ratio*(sweep_high - absorb_extreme)
        if (
            use_wick_fill
            and sweep_extreme
            and absorb_extreme
            and 0.0 <= wick_fill_ratio <= 1.0
        ):
            try:
                if side == "BUY":
                    anchor = float(sweep_extreme)
                    target = float(absorb_extreme)
                    entry_wick = anchor + wick_fill_ratio * (target - anchor)
                else:
                    anchor = float(sweep_extreme)
                    target = float(absorb_extreme)
                    entry_wick = anchor - wick_fill_ratio * (anchor - target)

                # On applique un léger buffer côté sécurité
                entry_wick = float(entry_wick)
                if side == "BUY":
                    entry_wick = min(entry_wick + buf, max(entry_wick, target))
                else:
                    entry_wick = max(entry_wick - buf, min(entry_wick, target))

                if entry_wick > 0:
                    return round(entry_wick, 10)
            except Exception:
                # On ignore et on retombe sur la logique standard
                pass

        # --- 1) Break of absorption extreme ---
        if trigger == "break_of_absorption_extreme":
            ref = float(absorb_extreme) if absorb_extreme else float(price_now)
            price = (ref + buf) if side == "BUY" else (ref - buf)
            return round(price, 10) if price > 0 else None

        # --- 2) Retracement (OB/FVG), avec confluence et MTF ---
        if trigger in {"retracement", "ob_or_fvg_retest", "retest"}:
            # Zones LTF
            ob_band = zone_band_from_obj(sig.get("ob_details"))
            fvg_band = zone_band_from_obj(sig.get("fvg_details"))

            # Zones MTF (si dispo et autorisé)
            if use_mtf_zone:
                ob_band_htf = zone_band_from_obj(
                    sig.get("ob_details_htf")
                    or sig.get("ob_mtf")
                    or sig.get("ob_higher_tf")
                )
                fvg_band_htf = zone_band_from_obj(
                    sig.get("fvg_details_htf")
                    or sig.get("fvg_mtf")
                    or sig.get("fvg_higher_tf")
                )
            else:
                ob_band_htf = None
                fvg_band_htf = None

            # 2.a Confluence OB∩FVG (LTF d'abord)
            confl_band = None
            if prefer_confluent and ob_band and fvg_band:
                confl_band = zone_overlap(ob_band, fvg_band)

            # 2.b Confluence MTF si pas de confluence LTF
            if prefer_confluent and confl_band is None and ob_band_htf and fvg_band_htf:
                confl_band = zone_overlap(ob_band_htf, fvg_band_htf)

            # 2.c Liste des candidats (ordre de priorité)
            candidates_prices: List[float] = []

            if confl_band:
                candidates_prices.append(side_entry_edge(confl_band, side))
            # Sinon OB/FVG LTF
            if ob_band:
                candidates_prices.append(side_entry_edge(ob_band, side))
            if fvg_band:
                candidates_prices.append(side_entry_edge(fvg_band, side))
            # Puis OB/FVG HTF si demandé
            if use_mtf_zone:
                if ob_band_htf:
                    candidates_prices.append(side_entry_edge(ob_band_htf, side))
                if fvg_band_htf:
                    candidates_prices.append(side_entry_edge(fvg_band_htf, side))

            # Choix du meilleur candidat (plus proche dans le bon sens)
            level = nearest_valid_retracement(side, candidates_prices, float(price_now))
            if level is not None and level > 0:
                return round(float(level), 10)

            # fallback retracement: si on a un extrême d'absorption, utiliser le break
            if absorb_extreme:
                ref = float(absorb_extreme)
                price = (ref + buf) if side == "BUY" else (ref - buf)
                return round(price, 10) if price > 0 else None

            return None

        # --- 3) Fallback par défaut -> break ---
        if absorb_extreme:
            ref = float(absorb_extreme)
        else:
            ref = float(price_now)
        price = (ref + buf) if side == "BUY" else (ref - buf)
        return round(price, 10) if price > 0 else None

    def _first_retracement_level(
        self, sig: Dict[str, Any], side: str
    ) -> Optional[float]:
        """
        Récupère un niveau de retracement pertinent (OB > FVG).
        On lit ob_details / fvg_details (format dict ou liste).
        Retourne le prix 'limite' à tester.
        """
        # OB prioritaire
        ob = sig.get("ob_details")
        price = self._extract_zone_price(ob, side)
        if price is not None:
            return price

        # FVG ensuite
        fvg = sig.get("fvg_details")
        price = self._extract_zone_price(fvg, side)
        if price is not None:
            return price

        return None

    def _extract_zone_price(self, obj: Any, side: str) -> Optional[float]:
        """
        Essaie d'extraire un prix 'limite' d'une zone (OB/FVG), en prenant:
        - pour BUY: bord inférieur de la zone
        - pour SELL: bord supérieur de la zone
        Supporte dict ou liste de dicts.
        """
        if isinstance(obj, dict):
            return self._zone_edge_from_dict(obj, side)
        if isinstance(obj, list) and len(obj) > 0:
            # on essaye le dernier/plus récent
            for x in reversed(obj):
                if isinstance(x, dict):
                    val = self._zone_edge_from_dict(x, side)
                    if val is not None:
                        return val
        return None

    def _zone_edge_from_dict(self, d: Dict[str, Any], side: str) -> Optional[float]:
        """
        Cherche 'zone_high'/'zone_low' ou 'upper'/'lower' dans un dict.
        """
        keys_hi = ("zone_high", "upper", "high", "hi")
        keys_lo = ("zone_low", "lower", "low", "lo")
        z_hi = None
        z_lo = None
        for k in keys_hi:
            if k in d and d[k] is not None:
                z_hi = float(d[k])
                break
        for k in keys_lo:
            if k in d and d[k] is not None:
                z_lo = float(d[k])
                break
        if side == "BUY" and z_lo is not None:
            return z_lo
        if side == "SELL" and z_hi is not None:
            return z_hi
        return None

    def _compute_sl(
        self,
        side: str,
        sig: Dict[str, Any],
        entry_price: float,
        buffer_pips: float = 2.0,
    ) -> Optional[float]:
        """
        Stop-loss institutionnel (strict Liquidity):
        - SL initial = extrême du sweep ± buffer
        - Trailing structurel activé si config l’autorise :
            • BUY: sous dernier HL ou OB valide
            • SELL: au-dessus dernier LH ou OB valide
        - ❌ Aucun fallback ATR : si pas de niveau Liquidity → retourne None
        """
        try:
            pip_size = float(sig.get("point", 0.0001)) * 10.0
        except Exception:
            pip_size = 0.0001
        buf = buffer_pips * pip_size

        sl_price = None

        # === 1) Sweep extrême ===
        sweep_extreme = None
        sd = sig.get("sweep_details")
        if isinstance(sd, dict) and "extreme_price" in sd:
            sweep_extreme = float(sd["extreme_price"])
        elif isinstance(sd, list):
            for x in reversed(sd):
                if isinstance(x, dict) and x.get("present") and x.get("extreme_price"):
                    sweep_extreme = float(x["extreme_price"])
                    break

        if sweep_extreme:
            if side == "BUY":
                sl_price = sweep_extreme - buf
            elif side == "SELL":
                sl_price = sweep_extreme + buf

        # === 2) Trailing structurel (si activé en config) ===
        trailing_cfg = self._cfg_dict("liquidity_exit.trailing_structure", {})
        if trailing_cfg.get("enabled", False):
            # BOS/MSS
            bos = sig.get("bos_mss_details")
            if isinstance(bos, dict) and bos.get("swing_point"):
                bos_level = float(bos.get("swing_point"))
                if side == "BUY" and bos_level and bos_level > 0:
                    sl_price = max(sl_price or 0, bos_level - buf)
                elif side == "SELL" and bos_level and bos_level > 0:
                    sl_price = min(sl_price or 1e9, bos_level + buf)

            # Order Block
            ob = sig.get("ob_details")
            if isinstance(ob, dict):
                if side == "BUY" and ob.get("zone_low"):
                    sl_price = max(sl_price or 0, float(ob["zone_low"]) - buf)
                elif side == "SELL" and ob.get("zone_high"):
                    sl_price = min(sl_price or 1e9, float(ob["zone_high"]) + buf)

        # === Résultat strict ===
        if not sl_price:
            self.logger.warning(
                "[LIQUIDITY] ❌ Aucun SL Liquidity identifié (sweep/OB/BOS)."
            )
            return None

        self.logger.info(f"[LIQUIDITY] 🛡️ Stop-loss défini: {sl_price:.5f}")
        return round(sl_price, 10)

    def _compute_tp(
        self,
        side: str,
        sig: Dict[str, Any],
        entry_price: float,
        sl_price: float,
        confidence: float = 0.7,
    ) -> Optional[List[float]]:
        """
        TP institutionnel (strict Liquidity) :
        - TP1 = cluster de liquidité le plus proche (EQH/EQL > OB > FVG)
        - TP2 = niveau plus éloigné (2e cluster, OB ou FVG suivant)
        - ❌ Aucun fallback RR : si pas de cible Liquidity → retourne None
        """
        try:
            pip_size = float(sig.get("point", 0.0001)) * 10.0
        except Exception:
            pip_size = 0.0001

        candidates = []

        # === EQH/EQL ===
        eqh_eql = sig.get("eqh_eql_details")
        if isinstance(eqh_eql, list) and eqh_eql:
            for lvl in eqh_eql:
                if not isinstance(lvl, dict):
                    continue
                price = lvl.get("level_price")
                direction = str(lvl.get("direction", "")).lower()
                if not price:
                    continue
                if side == "BUY" and "high" in direction:
                    candidates.append(("EQH", float(price)))
                elif side == "SELL" and "low" in direction:
                    candidates.append(("EQL", float(price)))

        # === OB ===
        ob = sig.get("ob_details")
        if ob and isinstance(ob, dict):
            lvl = ob.get("zone_high") if side == "BUY" else ob.get("zone_low")
            if lvl:
                candidates.append(("OB", float(lvl)))

        # === FVG ===
        fvg = sig.get("fvg_details")
        if fvg and isinstance(fvg, dict):
            lvl = fvg.get("zone_high") if side == "BUY" else fvg.get("zone_low")
            if lvl:
                candidates.append(("FVG", float(lvl)))

        # === Filtrage directionnel & tri ===
        if side == "BUY":
            candidates = [(tag, lvl) for tag, lvl in candidates if lvl > entry_price]
            candidates.sort(key=lambda x: x[1])  # plus proche en premier
        else:
            candidates = [(tag, lvl) for tag, lvl in candidates if lvl < entry_price]
            candidates.sort(key=lambda x: x[1], reverse=True)

        # === Résultat strict ===
        if not candidates:
            self.logger.warning(
                "[LIQUIDITY] ❌ Aucun TP Liquidity identifié (EQH/EQL, OB, FVG)."
            )
            return None

        tp1 = candidates[0][1]  # cible la plus proche
        tp2 = candidates[1][1] if len(candidates) > 1 else None

        tp_list = [round(tp1, 10)]
        if tp2:
            tp_list.append(round(tp2, 10))

        self.logger.info(f"[LIQUIDITY] 🎯 TP Liquidity choisis: {tp_list}")
        return tp_list

    def _nearest_liquidity_level(
        self, side: str, sig: Dict[str, Any]
    ) -> Optional[float]:
        """
        Sélectionne la cible de liquidité la plus pertinente dans la direction du trade.
        Priorité:
          1. Cluster EQH/EQL
          2. Order Block
          3. Fair Value Gap
        Fallback: None (la stratégie utilisera smart_targets ATR si rien trouvé).
        """
        candidates = []

        # === 1) EQH / EQL (clusters)
        eqh_eql = sig.get("eqh_eql_details")
        if isinstance(eqh_eql, list) and eqh_eql:
            levels = []
            for lvl in eqh_eql:
                if not isinstance(lvl, dict):
                    continue
                price = lvl.get("level_price")
                if not price:
                    continue
                direction = lvl.get("direction", "").lower()
                if (side == "BUY" and "high" in direction) or (
                    side == "SELL" and "low" in direction
                ):
                    levels.append(float(price))

            if levels:
                # cluster = moyenne des niveaux proches (< 5 pips d’écart)
                pip_size = float(sig.get("point", 0.0001)) * 10.0
                levels = sorted(levels)
                cluster = [levels[0]]
                for p in levels[1:]:
                    if abs(p - cluster[-1]) <= 5 * pip_size:
                        cluster.append(p)
                    else:
                        break
                cluster_level = sum(cluster) / len(cluster)
                candidates.append(("EQH/EQL", cluster_level))

        # === 2) Order Block
        ob = sig.get("ob_details")
        lvl = self._zone_target(side, ob)
        if lvl:
            candidates.append(("OB", lvl))

        # === 3) Fair Value Gap
        fvg = sig.get("fvg_details")
        lvl = self._zone_target(side, fvg)
        if lvl:
            candidates.append(("FVG", lvl))

        if not candidates:
            return None

        # --- Tri par priorité (EQH/EQL > OB > FVG) ---
        priority = {"EQH/EQL": 0, "OB": 1, "FVG": 2}
        candidates.sort(key=lambda x: priority.get(x[0], 99))

        best = candidates[0]
        self.logger.info(f"[LIQUIDITY] 🎯 Cible choisie ({best[0]}): {best[1]:.5f}")
        return best[1]

    def _eqh_eql_target(self, side: str, eqh_eql) -> Optional[float]:
        """
        Cherche un niveau EQH/EQL exploitable côté cible.
        eqh_eql peut être dict ou liste; retourne un niveau (float) si pertinent.
        - BUY: viser un EQH (liquidité au-dessus)
        - SELL: viser un EQL (liquidité en-dessous)
        """
        target_type = "eqh" if side == "BUY" else "eql"
        if isinstance(eqh_eql, dict):
            if str(eqh_eql.get("type", "")).lower() == target_type and eqh_eql.get(
                "level"
            ):
                return float(eqh_eql["level"])
        if isinstance(eqh_eql, list) and len(eqh_eql) > 0:
            for x in reversed(eqh_eql):
                if (
                    isinstance(x, dict)
                    and str(x.get("type", "")).lower() == target_type
                    and x.get("level")
                ):
                    return float(x["level"])
        return None

    def _zone_target(self, side: str, obj) -> Optional[float]:
        """
        Retourne un bord de zone (OB/FVG) dans la direction TP.
        BUY -> viser bord supérieur ; SELL -> bord inférieur (on va chercher la liquidité opposée).
        """
        if isinstance(obj, dict):
            return self._zone_tp_from_dict(obj, side)
        if isinstance(obj, list) and len(obj) > 0:
            for x in reversed(obj):
                if isinstance(x, dict):
                    val = self._zone_tp_from_dict(x, side)
                    if val is not None:
                        return val
        return None

    def _zone_tp_from_dict(self, d: Dict[str, Any], side: str) -> Optional[float]:
        keys_hi = ("zone_high", "upper", "high", "hi")
        keys_lo = ("zone_low", "lower", "low", "lo")
        z_hi = None
        z_lo = None
        for k in keys_hi:
            if k in d and d[k] is not None:
                z_hi = float(d[k])
                break
        for k in keys_lo:
            if k in d and d[k] is not None:
                z_lo = float(d[k])
                break
        if side == "BUY" and z_hi is not None:
            return z_hi
        if side == "SELL" and z_lo is not None:
            return z_lo
        return None

    def _extract_atr(
        self, sig: Dict[str, Any], period: int, pip_size: float
    ) -> Optional[float]:
        """
        Extrait (ou approxime) un ATR en prix.
        - Si sig fournit 'atr_pips' (ex: via bollinger.atr_pips): convertit en prix
        - Sinon essaye 'atr_value' déjà en prix
        """
        atr_pips = sig.get("atr_pips")
        if atr_pips is not None:
            try:
                return float(atr_pips) * pip_size
            except Exception:
                pass
        atr_val = sig.get("atr_value")
        if atr_val is not None:
            try:
                return float(atr_val)
            except Exception:
                pass
        return None

    def _estimate_rr(
        self,
        entry_price: float,
        sl_price: float,
        tp_prices: Optional[List[float]],
        sig: Dict[str, Any],
        confidence: float = 0.7,
    ) -> Optional[float]:
        """
        Estime le RR institutionnel (strict Liquidity).
        - RR = |TP - Entry| / |Entry - SL|
        - Pondéré par confiance et confluence
        - ❌ Aucun fallback si TP/SL manquant
        """
        if not entry_price or not sl_price or not tp_prices:
            self.logger.warning(
                "[LIQUIDITY] ❌ Impossible de calculer RR (entry/sl/tp manquant)."
            )
            return None

        try:
            # Distance risque (SL)
            risk = abs(entry_price - sl_price)
            if risk <= 0:
                return None

            # Distance reward (on prend TP le plus éloigné)
            reward = max(abs(tp - entry_price) for tp in tp_prices if tp)
            if reward <= 0:
                return None

            rr = reward / risk

            # --- Pondération par confiance ---
            if confidence < 0.6:
                rr *= 0.7
            elif confidence > 0.8:
                rr *= 1.1

            # --- Bonus confluence ---
            fvg = bool(sig.get("fvg_detected"))
            ob = bool(sig.get("ob_detected"))
            bos = bool(sig.get("bos_mss_detected"))
            if fvg and ob and bos:
                rr *= 1.1

            rr = round(rr, 2)
            self.logger.info(f"[LIQUIDITY] 📊 RR estimé: {rr}")
            return rr

        except Exception as e:
            self.logger.warning(f"[LIQUIDITY] Erreur calcul RR: {e}")
            return None

    # =========================
    #  DECISION PACKAGE (OUT)
    # =========================
    def _build_decision_package_from_proposal(
        self, asset: str, p: Dict[str, Any]
    ) -> Dict[str, Any]:
        """
        Construit un package d'ordre complet pour l'executor.
        On inclut à la fois prix absolus et fallback pips (compat descendante).
        """
        # fallback pips (au cas où l’executor en aurait besoin)
        target_tp_pips = None
        target_sl_pips = None
        try:
            if p.get("tp_price") and p.get("entry_price"):
                target_tp_pips = abs(p["tp_price"] - p["entry_price"])
            if p.get("sl_price") and p.get("entry_price"):
                target_sl_pips = abs(p["entry_price"] - p["sl_price"])
        except Exception:
            pass

       # Rule name plus souple (si défini dans la proposition, sinon valeur par défaut)
        rule_name = p.get("rule_name") or "liquidity_entry"

        decision = {
            "action": p.get("action"),
            "asset": asset,
            "order_type": p.get("order_type", "LIMIT"),
            "strategy_type": self.strategy_config.get("strategy_name", "liquidity"),
            "rule_name": rule_name,
            "magic_number": self.strategy_config.get("magic_number"),
            # Params d'exécution
            "entry_price": p.get("entry_price"),
            "sl_price": p.get("sl_price"),
            "tp_price": p.get("tp_price"),
            "timeout_bars": p.get("timeout_bars"),
            "use_mitigation": p.get("use_mitigation"),
            # Compat descendante (si l'executor attend encore des 'pips')
            "target_tp_pips": target_tp_pips,
            "target_sl_pips": target_sl_pips,
            # Métadonnées utiles pour logs et audit
            "confidence": p.get("confidence", 0.0),
        }
        return decision

    # =========================
    #     CONFIG HELPERS
    # =========================
    def _cfg(self, path: str, default: Any = None) -> Any:
        """Lit un scalaire depuis strategy_config ou config_manager."""
        # 1) essayer strategy_config (chemin 'a.b.c')
        cur = self.strategy_config
        for part in path.split("."):
            if not isinstance(cur, dict) or part not in cur:
                cur = None
                break
            cur = cur[part]
        if cur is not None:
            return cur

        # 2) sinon config_manager global
        try:
            if self.config_manager:
                val = self.config_manager.get(path, default)
                return default if val is None else val
        except Exception:
            pass
        return default

    def _cfg_dict(
        self, path: str, default: Optional[Dict[str, Any]] = None
    ) -> Dict[str, Any]:
        val = self._cfg(path, default=default or {})
        return val if isinstance(val, dict) else (default or {})

    def _cfg_list(self, path: str, default: Optional[List[Any]] = None) -> List[Any]:
        val = self._cfg(path, default=default or [])
        return val if isinstance(val, list) else (default or [])
