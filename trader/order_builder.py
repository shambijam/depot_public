# trader/order_builder.py - Module de Construction d'Ordres pour le Bot SNIPER_X
from __future__ import annotations

import math
import re
import uuid
from datetime import datetime, timezone, timedelta
from typing import Any, Dict, Optional
from trader.errors import TradeExecutionError
from trader.sizing import _calculate_risk_based_volume as _sizing_risk_volume
from trader.sltp import (
    resolve_side,
    _calculate_sl_tp_prices,
    _resolve_basket_context_for_sltp,
)


# --- FLOW/VOL GATE (soft) ----------------------------------------------------
def _passes_flow_vol_gate(
    self,
    symbol: str,
    action: str,
    symbol_info,
    market_context: dict,
    active_config: dict,
):
    """
    Garde-fou SOUPLE: on ne bloque le trade que si au moins 2 conditions
    sont franchement mauvaises. Données lues depuis market_context.
    - tick_rate (footprint M1)
    - score footprint et/ou score orderflow
    - ATR M1 (en pips, si dispo)
    - direction (CVD/delta) alignée à BUY/SELL (soft)
    """

    def _num(x, d=None):
        try:
            return float(x)
        except Exception:
            return d

    # pip_size (compat or): pour XAU (digits=2) → 1 pip = 1 point ; EURUSD (digits=5) → 1 pip = 10 points
    try:
        point = _num(getattr(symbol_info, "point", 0.0001), 0.0001)
        digits = int(getattr(symbol_info, "digits", 5) or 5)
        pip_size = point * (10.0 if digits in (3, 5) else 1.0)
    except Exception:
        pip_size = 0.01

    # --- Config très permissive par défaut (pour éviter un bot muet) ---
    gate_cfg = (
        ((active_config.get("entry_rules") or {}).get("scalping") or {}).get(
            "flow_vol_gate", {}
        )
    ) or {}
    enabled = bool(gate_cfg.get("enabled", True))  # activé par défaut (mode soft)
    mode = str(gate_cfg.get("mode", "soft")).lower()
    per_asset = gate_cfg.get("per_asset", {}) or {}
    aset = per_asset.get(symbol.upper(), {})

    # Seuils généraux (modérés) + overrides par actif possibles
    min_tick_rate = _num(
        aset.get("min_tick_rate", gate_cfg.get("min_tick_rate", 0.8))
    )  # 0.8 t/s par défaut
    min_fp_score = _num(
        aset.get("min_footprint_score", gate_cfg.get("min_footprint_score", 65.0))
    )
    min_of_score = _num(
        aset.get("min_orderflow_score", gate_cfg.get("min_orderflow_score", 60.0))
    )
    min_atr_m1_pips = _num(
        aset.get("min_atr_m1_pips", gate_cfg.get("min_atr_m1_pips", 0.0))
    )  # 0 = ignoré
    dir_filter = bool(gate_cfg.get("dir_filter", True))
    dir_strength_ratio = _num(
        gate_cfg.get("dir_strength_ratio", 0.30)
    )  # soft: n’exige l’alignement que si opposition “forte”
    dir_strength_abs = _num(
        gate_cfg.get("dir_strength_abs", 10.0)
    )  # delta/CVD absolu minimal pour considérer “fort”
    min_fails_to_block = int(
        gate_cfg.get("min_fails_to_block", 2)
    )  # clé de la souplesse: il faut ≥2 KO pour bloquer

    if not enabled or mode == "off":
        self.logger.info(f"[FLOW-GATE] disabled/off for {symbol}.")
        return True, "disabled"

    # --- Récup des métriques depuis market_context (robuste aux structures) ----
    mc = market_context or {}
    sym = symbol.upper()

    # Footprint
    fp = (
        ((mc.get("trading_signals") or {}).get(sym) or {}).get("footprint")
        or (mc.get("footprint") or {}).get(sym)
        or (mc.get("footprints") or {}).get(sym)
        or {}
    )
    fp_score = _num(fp.get("score"))
    tick_rate = _num(fp.get("tick_rate"))
    delta_total = _num(fp.get("delta_total"))
    total_vol = _num(fp.get("total_volume"))

    # Orderflow
    of = (
        ((mc.get("trading_signals") or {}).get(sym) or {}).get("orderflow")
        or (mc.get("orderflow") or {}).get(sym)
        or {}
    )
    of_score = _num(of.get("score"))
    cvd = _num(of.get("cvd") or of.get("CVD"))
    if total_vol is None:
        total_vol = _num(of.get("total_volume"))

    # ATR M1 (essaye plusieurs clés usuelles)
    md = (mc.get("market_data") or {}).get(sym) or {}
    atr_candidates = [
        md.get("atr_m1"),
        md.get("ATR_M1"),
        md.get("atr_14_m1"),
        md.get("atr_last_m1"),
        md.get("atr_m1_price"),
    ]
    atr_m1 = None
    for v in atr_candidates:
        atr_m1 = _num(v)
        if atr_m1:
            break
    atr_m1_pips = None
    if atr_m1 and pip_size and pip_size > 0:
        atr_m1_pips = atr_m1 / pip_size  # convertit prix → pips

    # --- Évaluations (soft) ----------------------------------------------------
    fails = []
    info = {}

    if tick_rate is not None:
        info["tick_rate"] = tick_rate
        if min_tick_rate and tick_rate < float(min_tick_rate):
            fails.append(f"tick_rate<{min_tick_rate}")
    if fp_score is not None:
        info["fp_score"] = fp_score
        if min_fp_score and fp_score < float(min_fp_score):
            fails.append(f"fp_score<{min_fp_score}")
    if of_score is not None:
        info["of_score"] = of_score
        if min_of_score and of_score < float(min_of_score):
            fails.append(f"of_score<{min_of_score}")
    if atr_m1_pips is not None and min_atr_m1_pips and float(min_atr_m1_pips) > 0:
        info["atr_m1_pips"] = atr_m1_pips
        if atr_m1_pips < float(min_atr_m1_pips):
            fails.append(f"atr_m1<{min_atr_m1_pips}pips")

    # Direction (soft): on bloque seulement si opposition “forte”
    if dir_filter:
        dir_sign = 1.0 if str(action).upper() == "BUY" else -1.0
        direction_val = cvd if cvd is not None else delta_total
        if direction_val is not None:
            info["dir_metric"] = direction_val
            oppo = (dir_sign * direction_val) < 0
            strong = False
            if total_vol and total_vol > 0:
                if abs(direction_val) / float(total_vol) >= float(dir_strength_ratio):
                    strong = True
            if abs(direction_val) >= float(dir_strength_abs):
                strong = True
            if oppo and strong:
                fails.append("dir_opposition_strong")

    # Décision soft
    if len(fails) >= max(1, min_fails_to_block):
        self.logger.info(
            f"[FLOW-GATE][REJECT] {symbol} action={action} fails={fails} info={info}"
        )
        return False, {"fails": fails, "info": info}
    else:
        self.logger.info(f"[FLOW-GATE][PASS] {symbol} action={action} info={info}")
        return True, {"info": info}


def prepare_order(self, decision_package: dict) -> dict:
    """
    Calcule et prépare la demande d'ordre complète pour MetaTrader 5.
    Zéro tolérance aux valeurs 'UNKNOWN' : on normalise et on valide avant toute requête MT5.

    ✅ Décision unique du volume
    - Le volume est TOUJOURS calculé via `_calculate_risk_based_volume(...)`.
    - Tout volume présent dans la décision est ignoré.
    - La normalisation broker fait un FLOOR (jamais d'augmentation) pour ne pas dépasser le budget.

    ✅ Burst scalping (nouveau cahier des charges SL/TP)
    - Exécution master **MARKET** (split géré par l'exécuteur/pipeline).
    - `burst_size` résolu (decision → conf) et propagé.
    - `sizing_scope="BASKET"` pour que le sizing fasse `risk_per_trade_percent / burst_size`.
    - SL **OBLIGATOIRE** et **TP ACTIF** pour le burst (plus de trailing-only).
      → SL/TP **identiques en distance** pour tous les tickets du panier.
      → RR dynamique possible via hints (ex: `tp_rr_ratio_hint`) consommés dans `_calculate_sl_tp_prices`.
    """

    self.logger.info("Préparation de l'ordre MT5...")

    # --- Unpack sûrs pour éviter les UnboundLocalError ---
    trade_decision = (decision_package or {}).get("trade_decision", {}) or {}
    market_context = (decision_package or {}).get("market_context", {}) or {}
    active_config = (decision_package or {}).get("active_config", {}) or {}

    # ---------- Helpers internes ----------
    def _first_non_empty(*vals):
        for v in vals:
            if isinstance(v, str) and v.strip():
                return v.strip()
        return None

    def _normalize_action(a: str) -> str:
        a = (a or "").strip().upper()
        mapping = {
            "BUY": "BUY",
            "SELL": "SELL",
            "LONG": "BUY",
            "SHORT": "SELL",
            "CLOSE": "CLOSE",
        }
        return mapping.get(a, "")

    def _normalize_volume(symbol_info, vol: float) -> float:
        """
        Clamp & FLOOR du volume selon les contraintes du symbole MT5.
        ⚠️ FLOOR au pas broker (jamais d'augmentation) pour ne pas dépasser le budget.
        """
        try:
            vmin = float(getattr(symbol_info, "volume_min", 0.01) or 0.01)
            vmax = float(getattr(symbol_info, "volume_max", 100.0) or 100.0)
            vstep = float(getattr(symbol_info, "volume_step", 0.01) or 0.01)
        except Exception:
            vmin, vmax, vstep = 0.01, 100.0, 0.01

        if not isinstance(vol, (int, float)) or vol <= 0:
            raise TradeExecutionError(f"Volume invalide pour normalisation ({vol}).")

        vol = max(vmin, min(vmax, float(vol)))
        if vstep > 0:
            steps = math.floor((vol - vmin) / vstep + 1e-12)
            vol = vmin + steps * vstep
            if vol > vmax:
                vol = vmax

        if vol < vmin:
            vol = vmin

        return round(vol, 8)

    def _resolve_key(dct, *names):
        """
        Retourne la première valeur non vide trouvée dans 'dct' parmi les clés données.
        Tolère les valeurs non-string (converties en str), ignore None / vides.
        """
        if not isinstance(dct, dict):
            return None
        for n in names:
            try:
                v = dct.get(n)
            except Exception:
                v = None
            if v is None:
                continue
            s = v if isinstance(v, str) else str(v)
            s = s.strip()
            if s:
                return s
        return None

    # ---------- 1) Action ----------
    final_decision = {}
    try:
        if isinstance(decision_package, dict):
            final_decision = (
                decision_package.get("final_decision")
                or decision_package.get("decision")
                or {}
            ) or {}
    except Exception:
        final_decision = {}

    action_raw = (
        _resolve_key(
            trade_decision,
            "final_action",
            "selected_action",
            "core_action",
            "action",
            "side",
            "direction",
        )
        or _resolve_key(
            final_decision,
            "final_action",
            "selected_action",
            "core_action",
            "action",
            "side",
            "direction",
        )
        or _resolve_key(
            decision_package,
            "final_action",
            "selected_action",
            "core_action",
            "action",
            "side",
            "direction",
        )
    )

    action = _normalize_action(action_raw)
    if not action:
        try:
            td_keys = list((trade_decision or {}).keys())
            fd_keys = list((final_decision or {}).keys())
        except Exception:
            td_keys, fd_keys = [], []
        self.logger.error(
            f"[ORDER_BUILDER] action introuvable | "
            f"trade_decision.keys={td_keys} | final_decision.keys={fd_keys} | "
            f"top_keys={list((decision_package or {}).keys())}"
        )
        raise TradeExecutionError(
            f"Action de trade invalide: '{action_raw}' (attendu: BUY/SELL/CLOSE)."
        )

    # ---------- 2) Asset ----------
    raw_symbol = (
        _first_non_empty(
            trade_decision.get("asset"),
            trade_decision.get("symbol"),
            trade_decision.get("instrument"),
        )
        or _first_non_empty(
            final_decision.get("asset"),
            final_decision.get("symbol"),
            final_decision.get("instrument"),
        )
        or _first_non_empty(
            (decision_package or {}).get("asset"),
            (decision_package or {}).get("symbol"),
            (decision_package or {}).get("instrument"),
        )
    )
    if not raw_symbol or raw_symbol.upper() == "UNKNOWN":
        msg = "Asset/symbole manquant ou 'UNKNOWN' dans la décision."
        self.logger.error(msg)
        raise TradeExecutionError(msg)

    # ---------- 3) Mapping broker ----------
    broker_map_acct = (
        market_context.get("active_broker_account", {}).get("symbol_map") or {}
    )
    broker_map_global = self.config_manager.get("asset_symbol_mapping", {}) or {}
    broker_symbol = (
        str(
            broker_map_acct.get(
                raw_symbol, broker_map_global.get(raw_symbol, raw_symbol)
            )
        )
        .strip()
        .upper()
    )
    if not broker_symbol or broker_symbol == "UNKNOWN":
        msg = f"Mapping broker invalide pour l'asset '{raw_symbol}' (résultat: '{broker_symbol}')."
        self.logger.error(msg)
        raise TradeExecutionError(msg)

    # ---------- [BURST GUARDRAILS] ----------
    try:
        rule_name = str(trade_decision.get("rule_name", "")).lower()
        if rule_name == "burst_scalping":
            burst_cfg = (
                (active_config.get("entry_rules", {}) or {}).get("scalping", {}) or {}
            ).get("burst_scalping", {}) or {}
            guard_cfg = burst_cfg.get("burst_guardrails", {}) or {}

            # PATCH BURST-COOL-KILL — désactivation totale des freins temps/panier
            max_open_positions = int(guard_cfg.get("max_open_positions", 5))
            cooldown_seconds = 0  # kill cooldown
            enforce_closure = bool(
                guard_cfg.get("enforce_burst_closure", False)
            )  # OFF par défaut
            single_burst_global = bool(
                guard_cfg.get("single_burst_global", False)
            )  # OFF par défaut

            import re, time

            def _field(obj, key, default=None):
                return (
                    obj.get(key, default)
                    if isinstance(obj, dict)
                    else getattr(obj, key, default)
                )

            # Scope (uniquement pour logs)
            if single_burst_global:
                all_open = self.mt5_connector.get_positions() or []
                scope_lbl = "global"
            else:
                all_open = self.mt5_connector.get_positions(symbol=broker_symbol) or []
                scope_lbl = broker_symbol

            # Parse des paniers (on conserve l’info pour debug, mais on ne bloque plus)
            open_burst_ids = set()
            for p in all_open:
                c = str(_field(p, "comment", "") or "")
                m = re.search(r"burst_scalping\|basket=([A-Za-z0-9_]+)", c)
                if m:
                    open_burst_ids.add(m.group(1))

            now_ts = time.time()
            last_burst_time = getattr(self, "_last_burst_time", 0)

            # (GUARD OFF) Ne plus bloquer si panier(s) déjà ouvert(s)
            # if enforce_closure and len(open_burst_ids) > 0:
            #     raise TradeExecutionError(
            #         f"⛔ Burst guard ({scope_lbl}): panier(s) en cours = {', '.join(sorted(open_burst_ids))} → interdit d’en démarrer un nouveau."
            #     )

            # (COOLDOWN OFF) Ne plus bloquer sur délai entre bursts
            # if cooldown_seconds > 0 and (now_ts - last_burst_time) < cooldown_seconds:
            #     raise TradeExecutionError(
            #         f"⏳ Cooldown actif ({now_ts - last_burst_time:.1f}s < {cooldown_seconds}s)."
            #     )

            self.logger.info(
                f"[BURST GUARD] bypass: cooldown=OFF, enforce_closure={enforce_closure}, "
                f"single_burst_global={single_burst_global}, scope={scope_lbl}"
            )
    except TradeExecutionError:
        raise
    except Exception as e:
        self.logger.warning(f"[BURST GUARD] Vérification partielle échouée: {e}")

    # ---------- 4) Cas CLOSE ----------
    if action == "CLOSE":
        return {
            "action": "CLOSE",
            "symbol": broker_symbol,
            "order_id": trade_decision.get("order_id", str(uuid.uuid4())),
            "ticket_to_close": trade_decision.get("ticket_to_close"),
        }

    # ---------- 5) order_type sécurisé ----------
    order_type = str(trade_decision.get("order_type", "MARKET")).upper()
    if order_type not in {"MARKET", "BUY_LIMIT", "SELL_LIMIT", "BUY_STOP", "SELL_STOP"}:
        self.logger.debug(f"order_type inconnu '{order_type}', fallback 'MARKET'.")
        order_type = "MARKET"

    try:
        # ---------- 6) Résolution + infos symbole ----------
        resolved_symbol = self.mt5_connector.resolve_broker_symbol(broker_symbol)
        if not resolved_symbol:
            msg = (
                f"Symbole MT5 introuvable pour '{broker_symbol}'. Vérifie Market Watch."
            )
            self.logger.error(msg)
            raise TradeExecutionError(msg)

        symbol_info = self.mt5_connector.get_symbol_info(resolved_symbol)
        if not symbol_info:
            msg = f"Symbole MT5 invalide ou introuvable ({resolved_symbol})."
            self.logger.error(msg)
            raise TradeExecutionError(msg)

        broker_symbol = resolved_symbol

        try:
            self.logger.info(
                f"[VOLUME] constraints broker {broker_symbol}: "
                f"min={getattr(symbol_info,'volume_min',None)}, "
                f"step={getattr(symbol_info,'volume_step',None)}, "
                f"max={getattr(symbol_info,'volume_max',None)}"
            )
        except Exception:
            pass

        # ---------- 6bis) Spread guard ----------
        try:
            spread_pips = float(self.mt5_connector.get_spread_pips(broker_symbol))
        except Exception:
            spread_pips = float("inf")

        entry_rules = (active_config.get("entry_rules") or {}).get("scalping") or {}
        cap_soft = entry_rules.get("max_spread_pips")
        cap_hard = entry_rules.get("hard_max_spread_pips")

        max_spread_cap = None
        if isinstance(cap_soft, (int, float)):
            max_spread_cap = float(cap_soft)
        if isinstance(cap_hard, (int, float)):
            max_spread_cap = (
                min(max_spread_cap, float(cap_hard))
                if max_spread_cap is not None
                else float(cap_hard)
            )

        if max_spread_cap is not None:
            if not math.isfinite(spread_pips) or spread_pips > max_spread_cap:
                raise TradeExecutionError(
                    f"Spread trop élevé: {spread_pips:.3f} pips > cap {max_spread_cap:.3f} pips."
                )
        # ---------- 6ter) FLOW/VOL SOFT GATE ----------
        ok_gate, why_gate = _passes_flow_vol_gate(
            self, broker_symbol, action, symbol_info, market_context, active_config
        )
        if not ok_gate:
            raise TradeExecutionError(f"FLOW/VOL gate: {why_gate}")

        # ---------- 7) Prix d'entrée ----------
        entry_price_market = self.mt5_connector.get_current_price(broker_symbol, action)
        if not entry_price_market or entry_price_market <= 0:
            raise TradeExecutionError(
                f"Impossible de récupérer un prix de marché valide pour {broker_symbol}."
            )

        entry_price_hint = trade_decision.get("entry_price")
        trigger_price = trade_decision.get("trigger_price")
        if trigger_price is None:
            if (
                order_type != "MARKET"
                and isinstance(entry_price_hint, (int, float))
                and entry_price_hint > 0
            ):
                trigger_price = entry_price_hint
            else:
                trigger_price = entry_price_market

        # Résolution `burst_size` (decision -> conf -> défaut)
        def _resolve_burst_size(fd: dict, cfg: dict) -> int:
            try:
                v = fd.get("burst_count") or fd.get("burst_size")
                if v is not None:
                    return int(v)
            except Exception:
                pass
            try:
                return int(
                    (((cfg or {}).get("entry_rules") or {}).get("scalping") or {})
                    .get("burst_scalping", {})
                    .get("burst_size", 1)
                    or 1
                )
            except Exception:
                return 1

        resolved_burst = _resolve_burst_size(trade_decision, active_config)
        try:
            resolved_burst = int(resolved_burst)
        except Exception:
            resolved_burst = 1
        if resolved_burst < 1:
            self.logger.warning("[BURST] burst_size<1 → forcé à 1")
            resolved_burst = 1

        trade_decision["burst_size"] = resolved_burst  # propagation utile au sizing
        _alias = str(trade_decision.get("rule_name", "")).lower().strip()
        if _alias in {
            "burst",
            "burst_master",
            "scalping_burst",
            "burst_single_master",
            "burst_single",
            "",
        }:
            trade_decision["rule_name"] = "burst_scalping"

        if trade_decision.get("rule_name") == "burst_scalping":
            trade_decision.setdefault("sizing_scope", "BASKET")
            trade_decision.setdefault("order_type", "MARKET")

        rule_name_local = str(trade_decision.get("rule_name", "")).lower()
        is_burst = rule_name_local == "burst_scalping"

        # --- PATCH A: normaliser et injecter l'action pour SL/TP ---
        try:
            side = resolve_side(
                trade_decision or final_decision or decision_package
            )  # → 'BUY' / 'SELL'
        except Exception as e:
            side = (
                action if isinstance(action, str) and action in ("BUY", "SELL") else ""
            )
            if not side:
                self.logger.error(
                    f"[ORDER_BUILDER] Action non résolue avant SL/TP: {e}"
                )
                raise

        trade_decision["action"] = side
        trade_decision["final_action"] = side
        self.logger.debug(f"[ORDER_BUILDER] Action normalisée pour SL/TP: {side}")

        # ---------- 7bis) SL/TP ----------
        # (Burst) Contexte panier frais pour guider le calcul (fill_ratio, PnL, phase, vol…)
        basket_ctx = None
        if is_burst:
            try:
                basket_ctx = _resolve_basket_context_for_sltp(
                    self,
                    trade_decision=trade_decision,
                    basket_context=None,
                    burst_manager=getattr(self, "burst_manager", None),
                    ttl_sec=2.0,
                )
            except Exception as e:
                try:
                    self.logger.debug(f"[ORDER_BUILDER] basket_ctx resolve failed: {e}")
                except Exception:
                    pass
                basket_ctx = None

        # Calcul desk-grade des niveaux (respect bid/ask, stops_level, RR dynamique…)
        try:
            sl_price, tp_price = _calculate_sl_tp_prices(
                self,
                trade_decision=trade_decision,
                config=active_config,
                symbol_info=symbol_info,
                entry_price=entry_price_market,
                market_context=market_context,
                basket_context=basket_ctx,  # important pour le burst
            )
        except Exception as e:
            self.logger.error(f"[ORDER_BUILDER] _calculate_sl_tp_prices error: {e}")
            raise TradeExecutionError(f"Échec calcul SL/TP: {e}")

        # Validation stricte du SL (obligatoire)
        if not (isinstance(sl_price, (int, float)) and sl_price > 0):
            raise TradeExecutionError("SL requis mais introuvable (calcul SL/TP).")

        # (facultatif) Trace/audit dans la décision
        try:
            trade_decision["sl_price"] = float(sl_price)
            if tp_price is not None:
                trade_decision["tp_price"] = float(tp_price)
        except Exception:
            pass

        # ---------- 8a) Sécurité broker & normalisation prix ----------
        try:
            point = float(getattr(symbol_info, "point", 0.0001) or 0.0001)
            tick = float(getattr(symbol_info, "trade_tick_size", point) or point)
            digits = int(
                getattr(symbol_info, "digits", max(0, round(-math.log10(point))))
            )

            stops_level_pts = int(getattr(symbol_info, "stops_level", 0) or 0)
            freeze_level_pts = int(getattr(symbol_info, "freeze_level", 0) or 0)
            broker_min = max(stops_level_pts, freeze_level_pts) * point  # en prix

            cfg_min_pips = None
            try:
                cfg_min_pips = (active_config.get("execution", {}) or {}).get(
                    "min_sl_tp_distance_pips", None
                ) or (active_config.get("scalping", {}) or {}).get(
                    "min_sl_tp_distance_pips", None
                )
            except Exception:
                cfg_min_pips = None

            pip_size = 10.0 * point
            cfg_min_price = (
                float(cfg_min_pips) * pip_size
                if isinstance(cfg_min_pips, (int, float))
                else 0.0
            )
            min_gap_price = max(3.0 * point, broker_min, cfg_min_price)

            def _ceil_to_tick(x: float) -> float:
                return round(math.ceil(x / tick) * tick, digits)

            def _floor_to_tick(x: float) -> float:
                return round(math.floor(x / tick) * tick, digits)

            has_tp = tp_price is not None

            if action == "BUY":
                if (entry_price_market - sl_price) < min_gap_price:
                    sl_price = entry_price_market - min_gap_price
                if has_tp and (tp_price - entry_price_market) < min_gap_price:
                    tp_price = entry_price_market + min_gap_price
                sl_price = _floor_to_tick(sl_price)
                if has_tp:
                    tp_price = _ceil_to_tick(tp_price)
                if not (sl_price < entry_price_market):
                    sl_price = _floor_to_tick(entry_price_market - min_gap_price)
                if has_tp and not (entry_price_market < tp_price):
                    tp_price = _ceil_to_tick(entry_price_market + min_gap_price)

            elif action == "SELL":
                if (sl_price - entry_price_market) < min_gap_price:
                    sl_price = entry_price_market + min_gap_price
                if has_tp and (entry_price_market - tp_price) < min_gap_price:
                    tp_price = entry_price_market - min_gap_price
                sl_price = _ceil_to_tick(sl_price)
                if has_tp:
                    tp_price = _floor_to_tick(tp_price)
                if not (entry_price_market < sl_price):
                    sl_price = _ceil_to_tick(entry_price_market + min_gap_price)
                if has_tp and not (tp_price < entry_price_market):
                    tp_price = _floor_to_tick(entry_price_market - min_gap_price)

            def _fmt(v):
                return (
                    f"{float(v):.{digits}f}" if isinstance(v, (int, float)) else "None"
                )

            self.logger.info(
                "[SAFETY] SL/TP normalisés | symbol=%s, entry=%s, SL=%s, TP=%s, min_gap=%s, stops_level=%s, freeze_level=%s",
                broker_symbol,
                _fmt(entry_price_market),
                _fmt(sl_price),
                _fmt(tp_price),
                _fmt(min_gap_price),
                str(stops_level_pts),
                str(freeze_level_pts),
            )
        except Exception as e:
            self.logger.warning(f"[SAFETY] Normalisation SL/TP échouée: {e}")

        # ---------- 8bis) RR minimum (soft, si TP présent) ----------
        try:
            rm_cfg = self.config_manager.get("risk_management") or {}
            min_rr_cfg = rm_cfg.get("min_rr", 0) or 0.0
            min_rr_hint = (
                trade_decision.get("min_rr_hint")
                or trade_decision.get("rr_min_hint")
                or ((active_config.get("risk_management") or {}).get("min_rr_hint"))
            )

            def _f(x):
                try:
                    return float(str(x).replace(",", "."))
                except Exception:
                    return None

            min_rr = _f(min_rr_hint)
            if min_rr is None:
                min_rr = _f(min_rr_cfg) or 0.0
        except Exception:
            min_rr = 0.0

        rr_value = None
        if tp_price is not None:
            eps = 1e-9
            if action == "BUY":
                risk = max(entry_price_market - sl_price, 0.0)
                reward = max(tp_price - entry_price_market, 0.0)
            else:  # SELL
                risk = max(sl_price - entry_price_market, 0.0)
                reward = max(entry_price_market - tp_price, 0.0)

            if risk <= eps or reward <= eps:
                rr_value = 0.0
                self.logger.warning(
                    f"⚠️ RR invalide (risk={risk:.6f}, reward={reward:.6f}) → accepté (permissif)."
                )
            else:
                rr_value = reward / risk
                self.logger.debug(
                    f"[RR] effectif={rr_value:.3f} (risk={risk:.6f}, reward={reward:.6f}, min={float(min_rr):.3f})"
                )
                if float(min_rr) > 0.0 and rr_value < float(min_rr):
                    self.logger.info(
                        f"ℹ️ RR insuffisant {rr_value:.2f} < min {float(min_rr):.2f} → accepté (permissif)."
                    )
            trade_decision["rr_effective"] = rr_value
        else:
            self.logger.debug("[RR] TP absent → RR non évalué (soft).")
            
        # ---------- 8c) Action SL/TP (résolution + whitelist) ----------
        # Politique:
        #  - Burst scalping: SL OBLIGATOIRE + TP OBLIGATOIRE → action "SET" (pose SL & TP)
        #  - Sinon: si TP absent → "SET_SL_ONLY" (scalping trailing-only possible ailleurs)
        #           si TP présent → "SET"
        _ALLOWED_SLTP = {"SET", "SET_SL_ONLY", "UPDATE", "NONE"}

        if is_burst:
            # Exigence docstring: "SL OBLIGATOIRE et TP ACTIF pour le burst"
            if tp_price is None:
                raise TradeExecutionError("Burst: TP requis mais absent (tp_method/TP calculé manquant).")
            sltp_action = "SET"
        else:
            sltp_action = "SET" if (tp_price is not None) else "SET_SL_ONLY"

        if sltp_action not in _ALLOWED_SLTP:
            raise TradeExecutionError(f"Action SL/TP non autorisée: {sltp_action}")

        # Propagation pour l'aval (logs, exécuteur, trailing manager, etc.)
        try:
            trade_decision["sltp_action"] = sltp_action
        except Exception:
            pass

        # ---------- 9) Volume via sizing risk-based (TOUJOURS exécuté) ----------
        account_trade_settings = (
            market_context.get("active_broker_account", {}).get("trade_settings", {})
            or {}
        )

        def _to_float(x):
            try:
                if isinstance(x, str):
                    xs = x.strip().replace("%", "").replace(",", ".")
                    return float(xs)
                return float(x)
            except Exception:
                return None

        def _cascade(*vals) -> float:
            for v in vals:
                f = _to_float(v)
                if f is not None and f > 0:
                    return f
            return 0.0

        import os

        resolved_risk_pct = _cascade(
            account_trade_settings.get("risk_per_trade_percent"),
            trade_decision.get("risk_per_trade_percent"),
            trade_decision.get("risk_pct"),
            trade_decision.get("risk_percent"),
            (active_config.get("sizing", {}) or {}).get("risk_per_trade_percent"),
            (active_config.get("risk_management", {}) or {}).get(
                "risk_per_trade_percent"
            ),
            self.config_manager.get("risk_management.risk_per_trade_percent"),
            self.config_manager.get("risk_management.default_risk_per_trade_percent"),
            self.config_manager.get("defaults.risk_per_trade_percent"),
            os.getenv("SNIPERX_RISK_PCT"),
            trade_decision.get("fallback_risk_per_trade_percent"),
            (active_config.get("risk_management", {}) or {}).get(
                "fallback_risk_per_trade_percent"
            ),
        )

        def _to_pos_float(x, default=None):
            try:
                if isinstance(x, str):
                    x = x.strip().replace(",", ".")
                v = float(x)
                return v if v > 0 else default
            except Exception:
                return default

        min_risk = _to_pos_float(
            self.config_manager.get("risk_management.min_risk_per_trade_percent", 0.01),
            0.01,
        )
        max_risk = _to_pos_float(
            self.config_manager.get("risk_management.max_risk_per_trade_percent", 2.0),
            2.0,
        )

        used_fallback = False
        if resolved_risk_pct <= 0:
            resolved_risk_pct = 0.25
            used_fallback = True

        if resolved_risk_pct < min_risk:
            self.logger.warning(
                f"[SIZING] risk% {resolved_risk_pct} < min {min_risk} → forcé à {min_risk}"
            )
            resolved_risk_pct = min_risk
        elif resolved_risk_pct > max_risk:
            self.logger.warning(
                f"[SIZING] risk% {resolved_risk_pct} > max {max_risk} → forcé à {max_risk}"
            )
            resolved_risk_pct = max_risk

        if used_fallback:
            self.logger.warning(
                f"[SIZING] Aucune source valide → fallback risk%={resolved_risk_pct} (configure `risk_per_trade_percent`)"
            )

        if not sl_price or sl_price <= 0:
            raise TradeExecutionError(
                f"Risk sizing impossible: sl_price invalide ({sl_price})"
            )

        sizing_scope = trade_decision.get("sizing_scope")
        if is_burst:
            sizing_scope = "BASKET"

        # --- Résolution STRICTE de l'équité du compte (obligatoire > 0) ---
        account_ctx = market_context.get("active_broker_account") or {}

        # Sources possibles (cascade prioritaire)
        equity_val = (
            # 0) Si déjà passé via trade_settings (rare mais supporté)
            account_trade_settings.get("equity")
            # 1) Contexte courant
            or account_ctx.get("equity")
            or (account_ctx.get("account_info") or {}).get("equity")
            or (account_ctx.get("info") or {}).get("equity")
        )

        # 2) Lecture MT5 directe si encore None
        if equity_val in (None, ""):
            try:
                ai = self.mt5_connector.get_account_info()
                equity_val = getattr(ai, "equity", None)
                if equity_val not in (None, ""):
                    self.logger.info(f"[SIZING] Équité résolue via MT5: {equity_val}")
            except Exception as e:
                self.logger.warning(
                    f"[SIZING] Impossible de lire l'équité via MT5: {e}"
                )

        # 3) Fallback de configuration (utile DEMO/dry-run)
        if equity_val in (None, ""):
            try:
                import os

                # 3a) Config manager
                equity_val = self.config_manager.get("risk_management.default_equity")
                # 3b) Active config
                if equity_val in (None, ""):
                    equity_val = (active_config.get("risk_management") or {}).get(
                        "default_equity"
                    )
                # 3c) Variable d'environnement
                if equity_val in (None, ""):
                    equity_val = os.getenv("SNIPERX_DEFAULT_EQUITY")
                if equity_val not in (None, ""):
                    self.logger.warning(
                        f"[SIZING] Fallback default_equity utilisé: {equity_val}"
                    )
            except Exception:
                equity_val = None

        # Normalisation & validation finale (>0 requis)
        def _as_pos_float(x):
            try:
                if isinstance(x, str):
                    x = x.strip().replace(",", ".")
                v = float(x)
                return v if v > 0 else None
            except Exception:
                return None

        equity_val = _as_pos_float(equity_val)

        if equity_val is None:
            # ⛔ BLOQUANT (évite l'erreur dans sizing.py)
            raise TradeExecutionError(
                "❌ Équité du compte invalide ou manquante.\n"
                "Solutions possibles :\n"
                "  1) Configure `risk_management.default_equity` dans ta config YAML\n"
                "  2) Définis la variable d'environnement SNIPERX_DEFAULT_EQUITY\n"
                "  3) Assure-toi que MT5 est connecté (get_account_info().equity)\n"
                "  4) En mode DEMO/dry-run, une equity par défaut est OBLIGATOIRE."
            )

        # Propagation stricte dans tous les contextes
        account_ctx["equity"] = equity_val
        ai = account_ctx.get("account_info") or {}
        ai["equity"] = equity_val
        account_ctx["account_info"] = ai
        market_context["active_broker_account"] = account_ctx

        self.logger.info(f"✅ [SIZING] Équité résolue et validée → {equity_val}")

        # --- Préparation du payload pour sizing.py ---
        # 1) Mise à jour du trade_settings du contexte
        ab = market_context.get("active_broker_account") or {}
        ts = dict((ab.get("trade_settings") or {}))
        ts["equity"] = equity_val  # ⬅ CRITIQUE
        ts["risk_per_trade_percent"] = resolved_risk_pct
        ab["trade_settings"] = ts
        market_context["active_broker_account"] = ab

        # 2) Payload EXPLICITE (prioritaire pour sizing.py)
        account_trade_settings_over = dict(account_trade_settings or {})
        account_trade_settings_over["risk_per_trade_percent"] = resolved_risk_pct
        account_trade_settings_over["equity"] = equity_val  # ⬅ OBLIGATOIRE

        # Guards anti-régression (sécurité supplémentaire)
        if account_trade_settings_over.get("equity") in (None, "", 0, 0.0):
            raise TradeExecutionError(
                "[SIZING] Guard: equity manquante dans le payload transmis à sizing.py"
            )
        if not sl_price or sl_price <= 0:
            raise TradeExecutionError(
                f"Risk sizing impossible: sl_price invalide ({sl_price})"
            )

        # Log de contrôle détaillé
        self.logger.info(
            f"📊 [SIZING] Inputs validés:\n"
            f"   ├─ equity={equity_val}\n"
            f"   ├─ risk%={resolved_risk_pct}\n"
            f"   ├─ scope={sizing_scope}\n"
            f"   ├─ burst_size={resolved_burst}\n"
            f"   └─ sl_price={sl_price}"
        )
        if "equity" not in account_trade_settings_over or account_trade_settings_over[
            "equity"
        ] in (None, "", 0):
            account_trade_settings_over["equity"] = 10000.0
            self.logger.warning("⚠️ Equity manquante, fallback à 10000")

        # 3) Calcul du lot (risk% / burst_size si scope BASKET)
        volume_final = float(
            _sizing_risk_volume(
                self,
                {
                    "action": action,
                    "asset": broker_symbol,
                    "order_type": "MARKET" if is_burst else order_type,
                    "confidence": trade_decision.get("confidence", 1.0),
                    "rule_name": trade_decision.get("rule_name"),
                    "volatility_factor": trade_decision.get("volatility_factor"),
                    "sizing_scope": sizing_scope,
                    "burst_size": resolved_burst,
                },
                active_config,
                market_context,
                symbol_info,
                entry_price_market,
                sl_price,
                account_trade_settings_over,  # ← contient equity > 0
            )
        )
        if (
            not isinstance(volume_final, (int, float))
            or not math.isfinite(volume_final)
            or volume_final <= 0
        ):
            raise TradeExecutionError(f"Lot calculé invalide: {volume_final!r}")

        # 4) Normalisation broker (FLOOR au pas) → ne jamais dépasser le budget
        volume_final = _normalize_volume(symbol_info, volume_final)
        self.logger.info(
            f"[SIZING] scope={sizing_scope} burst={resolved_burst} risk%={resolved_risk_pct} → "
            f"lot/ticket(normalisé)={volume_final}"
        )
        if not isinstance(volume_final, (int, float)) or volume_final <= 0:
            raise TradeExecutionError(
                f"Volume final invalide après normalisation: {volume_final}"
            )

        # ---------- 9b) Sécurités volume (fat-finger / caps) ----------
        try:
            tes = self.config_manager.get("trade_executor_settings", {}) or {}
            ff = tes.get("fat_finger_check", {}) or {}
            ff_enabled = bool(ff.get("enabled", False))
            vol_safety_enabled = bool(tes.get("volume_safety_enabled", False))

            def _to_pos_float(x):
                try:
                    if isinstance(x, str):
                        x = x.strip().replace(",", ".")
                    v = float(x)
                    return v if v > 0 else None
                except Exception:
                    return None

            # Contraintes compte (en plus des contraintes broker déjà appliquées)
            account_trade_settings_ctx = (
                market_context.get("active_broker_account", {}).get(
                    "trade_settings", {}
                )
                or {}
            )
            acc_min = _to_pos_float(account_trade_settings_ctx.get("min_lot"))
            acc_step = _to_pos_float(account_trade_settings_ctx.get("lot_step"))
            acc_max = _to_pos_float(account_trade_settings_ctx.get("max_lot"))
            self.logger.info(
                f"[VOLUME] constraints compte: min={acc_min}, step={acc_step}, max={acc_max}"
            )

            # Re-normalisation éventuelle au pas COMPTE (FLOOR uniquement — jamais d'augmentation)
            if acc_step and acc_step > 0:
                steps = math.floor(volume_final / acc_step + 1e-12)
                volume_final = round(steps * acc_step, 8)

            # utilitaire: floor au pas broker sans jamais augmenter (retourne None si < vmin)
            def _floor_broker(vol: float):
                try:
                    bmin = float(getattr(symbol_info, "volume_min", 0.01) or 0.01)
                    bmax = float(getattr(symbol_info, "volume_max", 100.0) or 100.0)
                    bstep = float(getattr(symbol_info, "volume_step", 0.01) or 0.01)
                except Exception:
                    bmin, bmax, bstep = 0.01, 100.0, 0.01
                if vol < bmin:
                    return None
                steps = math.floor((vol - bmin) / bstep + 1e-12)
                v = bmin + steps * bstep
                if v > bmax:
                    v = bmax
                return round(v, 8)

            # Politique de fat-finger: "REJECT" (défaut) ou "FLOOR" (réduction auto au cap)
            cap_policy = (
                (ff.get("policy") or tes.get("fat_finger_policy") or "REJECT")
                if isinstance(ff, dict)
                else "REJECT"
            )
            cap_policy = str(cap_policy).strip().upper()

            # Fat-finger par actif
            if ff_enabled:
                per_asset = ff.get("max_absolute_volume_for_asset") or {}
                cap_sym = _to_pos_float(per_asset.get(raw_symbol))
                if cap_sym is not None and volume_final > cap_sym:
                    if cap_policy in {"FLOOR", "CLAMP", "REDUCE"}:
                        target = min(volume_final, cap_sym)
                        new_vol = _floor_broker(target)
                        if new_vol is None:
                            # cap < min broker → impossible sans augmenter; on stoppe (sécurité)
                            raise TradeExecutionError(
                                f"Fat-finger: cap {cap_sym} < min lot broker → impossible de réduire sans augmenter."
                            )
                        self.logger.warning(
                            f"[FAT-FINGER] clamp: {volume_final} → {new_vol} (cap actif={cap_sym}, policy={cap_policy})"
                        )
                        volume_final = new_vol
                    else:
                        # Politique REJECT (comportement historique)
                        raise TradeExecutionError(
                            f"Fat-finger: volume {volume_final} > cap absolu {cap_sym} sur {raw_symbol}."
                        )

            # Cap global de sécurité
            cap_global = _to_pos_float(tes.get("max_absolute_volume_safety"))
            if (
                vol_safety_enabled
                and cap_global is not None
                and volume_final > cap_global
            ):
                raise TradeExecutionError(
                    f"Safety cap (global): volume {volume_final} > cap sécurité {cap_global}."
                )

            # Cap compte (max)
            if acc_max is not None and volume_final > acc_max:
                raise TradeExecutionError(
                    f"Volume {volume_final} > max lot compte {acc_max}."
                )

            # Min compte (politique: on n'augmente JAMAIS → on stoppe si en dessous)
            if acc_min is not None and volume_final < acc_min:
                raise TradeExecutionError(
                    f"Volume {volume_final} < min lot compte {acc_min} (politique: pas d’augmentation)."
                )

        except TradeExecutionError:
            raise
        except Exception as e:
            self.logger.warning(
                f"Vérif volume (fat-finger/caps) partielle échouée: {e}"
            )

        # ---------- 10) Construction requête ----------
        return self._build_mt5_request(
            {
                "action": action,
                "asset": broker_symbol,
                "order_type": "MARKET",  
                "rule_name": trade_decision.get("rule_name"),
                "comment": trade_decision.get("comment"),
                "sltp_action": sltp_action,
                "meta_rr_projected": (
                    trade_decision.get("meta_rr_projected")
                    or trade_decision.get("rr")
                    or trade_decision.get("rr_effective")
                ),
                "basket_id": trade_decision.get("basket_id"),
                "time_in_force": trade_decision.get("time_in_force"),
            },
            active_config,
            volume_final,  # ← volume déjà normalisé broker + éventuel pas compte
            entry_price_market,
            sl_price,
            tp_price,
            symbol_info,
            trigger_price,
            "MARKET",
        )

    except TradeExecutionError:
        raise
    except Exception as e:
        self.logger.error(
            f"Erreur inattendue préparation ordre {broker_symbol}: {e}", exc_info=True
        )
        raise TradeExecutionError(
            f"Échec inattendu de préparation d'ordre pour {broker_symbol}: {e}"
        ) from e


def _build_mt5_request(
    self,
    trade_decision: dict,
    config: dict,
    volume: float,
    entry_price_market: float,
    sl_price: float,
    tp_price: float,
    symbol_info: Any,
    trigger_price: Optional[float] = None,
    order_type_str: str = "MARKET",
) -> dict:
    """
    Construit et valide la requête finale pour l'API MetaTrader 5, en supportant
    Market / Limit / Stop, avec contrôles durcis (style desk).

    - Arrondis aux digits
    - Distances mini broker (stops_level / trade_stops_level)
    - Cohérence directionnelle prix/SL/TP (vs price_ref)
    - **Volume déjà normalisé en amont (validation only ici)**
    - Deviation/Filling policy robustes
    - Expiration (GTC/DAY/SPECIFIED + FOK-like pending)
    - Métadonnées d’audit & compliance_flags (ignorées par MT5)
    """
    # --- imports locaux sûrs ---
    import math, re, time
    from datetime import datetime, timezone, timedelta

    # ——— accès MT5 robuste ———
    mt5 = None
    try:
        import MetaTrader5 as _mt5  # type: ignore

        mt5 = _mt5
    except Exception:
        mt5 = getattr(getattr(self, "mt5_connector", None), "mt5", None)

    self.logger.info("Construction de la requête MT5 finale...")

    # --- Validations de base ---
    action_str = str(trade_decision.get("action", "")).upper()  # BUY / SELL
    expected_symbol = str(trade_decision.get("asset", "") or "N/A")

    if action_str not in ("BUY", "SELL"):
        raise TradeExecutionError(
            f"Action invalide: '{action_str}' (attendu BUY/SELL)."
        )

    if (
        (not symbol_info)
        or (not getattr(symbol_info, "name", None))
        or str(symbol_info.name).upper() == "UNKNOWN"
    ):
        raise TradeExecutionError(
            f"Symbole MT5 invalide ou non résolu (asset={expected_symbol}, symbol_info={getattr(symbol_info, 'name', 'None')})."
        )

    # Broker units
    digits = int(getattr(symbol_info, "digits", 0) or 0)
    point = float(getattr(symbol_info, "point", 0.0) or 0.0)
    if not (point > 0):
        raise TradeExecutionError("symbol_info.point invalide (<= 0).")

    # stops_level (clé broker tolérante)
    stops_lvl_points = float(
        getattr(symbol_info, "trade_stops_level", 0)
        or getattr(symbol_info, "stops_level", 0)
        or 0
    )
    min_stop_distance_price = stops_lvl_points * point

    # --- Volume (VALIDATION ONLY — déjà normalisé en amont) ---
    if not isinstance(volume, (int, float)) or not math.isfinite(volume) or volume <= 0:
        raise TradeExecutionError(f"Volume invalide ({volume}).")
    vol = float(volume)  # déjà normalisé en amont

    # --- Prix d’entrée & niveaux SL/TP arrondis ---
    if not isinstance(entry_price_market, (int, float)) or entry_price_market <= 0:
        raise TradeExecutionError("Prix d'entrée marché invalide.")
    entry_price_market = round(float(entry_price_market), digits)

    try:
        sl_price = round(float(sl_price), digits)
        if tp_price is not None:
            tp_price = round(float(tp_price), digits)
    except Exception as e:
        raise TradeExecutionError(f"SL/TP invalides: {e}")

    # --- Overrides absolus (si fournis dans la décision) ---
    if float(trade_decision.get("entry_price", 0) or 0) > 0:
        entry_price_market = round(float(trade_decision["entry_price"]), digits)
    if float(trade_decision.get("sl_price", 0) or 0) > 0:
        sl_price = round(float(trade_decision["sl_price"]), digits)
    if float(trade_decision.get("tp_price", 0) or 0) > 0:
        tp_price = round(float(trade_decision["tp_price"]), digits)

    # --- Mapping constantes MT5 (tolérant) ---
    if mt5 is None:
        raise TradeExecutionError("Module/constantes MT5 indisponibles.")

    # Actions
    mt5_action_deal = getattr(mt5, "TRADE_ACTION_DEAL", None)
    mt5_action_pending = getattr(mt5, "TRADE_ACTION_PENDING", None)
    if mt5_action_deal is None or mt5_action_pending is None:
        raise TradeExecutionError("Constantes MT5 TRADE_ACTION introuvables.")

    # Types marché
    ORDER_TYPE_BUY = getattr(mt5, "ORDER_TYPE_BUY", None)
    ORDER_TYPE_SELL = getattr(mt5, "ORDER_TYPE_SELL", None)
    if ORDER_TYPE_BUY is None or ORDER_TYPE_SELL is None:
        raise TradeExecutionError("Constantes MT5 ORDER_TYPE BUY/SELL introuvables.")

    # Mappings optionnels
    order_map = (getattr(self, "mt5_mappings", {}) or {}).get("order_types", {}) or {}
    fill_map = (getattr(self, "mt5_mappings", {}) or {}).get(
        "order_filling_policies", {}
    ) or {}
    time_map = (getattr(self, "mt5_mappings", {}) or {}).get(
        "order_time_flags", {}
    ) or {}

    # Time flags
    ORDER_TIME_GTC = getattr(mt5, "ORDER_TIME_GTC", None)
    ORDER_TIME_DAY = getattr(
        mt5, time_map.get("DAY", "ORDER_TIME_DAY"), getattr(mt5, "ORDER_TIME_DAY", None)
    )
    ORDER_TIME_SPECIFIED = getattr(
        mt5,
        time_map.get("SPECIFIED", "ORDER_TIME_SPECIFIED"),
        getattr(mt5, "ORDER_TIME_SPECIFIED", None),
    )
    if ORDER_TIME_GTC is None:
        raise TradeExecutionError("Constante MT5 ORDER_TIME_GTC introuvable.")

    # Filling policies
    ORDER_FILLING_FOK = getattr(
        mt5,
        fill_map.get("FOK", "ORDER_FILLING_FOK"),
        getattr(mt5, "ORDER_FILLING_FOK", None),
    )
    ORDER_FILLING_IOC = getattr(
        mt5,
        fill_map.get("IOC", "ORDER_FILLING_IOC"),
        getattr(mt5, "ORDER_FILLING_IOC", None),
    )
    if ORDER_FILLING_FOK is None or ORDER_FILLING_IOC is None:
        raise TradeExecutionError("Constantes MT5 ORDER_FILLING FOK/IOC introuvables.")

    # Policy par défaut (config)
    filling_policy_str = str(
        config.get("execution_policy", {}).get("type_filling", "FOK")
    ).upper()
    mt5_filling_policy = {
        "FOK": ORDER_FILLING_FOK,
        "IOC": ORDER_FILLING_IOC,
    }.get(filling_policy_str, ORDER_FILLING_FOK)

    # Déviation (points)
    deviation_points = int(
        config.get("execution_policy", {}).get("max_deviation_points", 20) or 20
    )
    deviation_points = max(0, deviation_points)

    # --- Burst: TP **autorisé** (plus d'ignorance du TP) ---
    rule = str(trade_decision.get("rule_name", "")).lower()
    is_burst = rule == "burst_scalping"
    has_tp = tp_price is not None and tp_price > 0

    # --- Basket/comment ---
    raw_comment = str(trade_decision.get("comment") or "")
    basket_id = str(trade_decision.get("basket_id") or "").strip() or None
    if not basket_id and raw_comment:
        m = re.search(r"basket=([A-Za-z0-9_]+)", raw_comment)
        if m:
            basket_id = m.group(1)
    if not basket_id:
        sym_name = getattr(symbol_info, "name", "") or expected_symbol
        basket_id = f"burst_{str(sym_name).upper()}"

    def _normalize_mt5_comment(text: str, max_len: int = 31) -> str:
        raw = (text or "").strip()
        raw = raw.replace("|", "").replace(" ", "")
        raw = re.sub(r"[^A-Za-z0-9._-]", "", raw)
        return raw[:max_len]

    comment = _normalize_mt5_comment(raw_comment or basket_id)

    # --- Construction base requête ---
    order_type_str = str(order_type_str or "MARKET").upper()
    request = {
        "symbol": symbol_info.name,
        "volume": float(vol),
        "magic": trade_decision.get(
            "magic_number",
            config.get(
                "magic_number",
                self.config_manager.get("trade_executor_settings", {}).get("magic", 0),
            ),
        ),
        "sl": float(sl_price),
        "type_time": ORDER_TIME_GTC,
        "deviation": int(deviation_points),
        "comment": comment,  # ≤31 chars, stable
        # — meta (ignorés par MT5) —
        "strategy_type": str(config.get("strategy_name", "unknown")).lower(),
        "rule_name": str(trade_decision.get("rule_name", "")),
        "meta_rr_projected": (
            trade_decision.get("meta_rr_projected")
            or trade_decision.get("rr")
            or trade_decision.get("rr_effective")
        ),
        "basket_id": basket_id,
        "is_burst_trade": is_burst,
    }
    if has_tp:
        request["tp"] = float(tp_price)  # TP actif si calculé

    # --- TIF pour MARKET (FOK/IOC si demandé dans la décision) ---
    if order_type_str == "MARKET":
        request["action"] = mt5_action_deal
        request["type"] = ORDER_TYPE_BUY if action_str == "BUY" else ORDER_TYPE_SELL
        request["price"] = entry_price_market
        request.setdefault(
            "type_filling", mt5_filling_policy
        )  # ne pas écraser un FOK/IOC explicite

    # --- FOK-like pour PENDING (LIMIT/STOP) ---
    elif order_type_str in {"BUY_LIMIT", "SELL_LIMIT", "BUY_STOP", "SELL_STOP"}:
        request["action"] = mt5_action_pending
        mapped = order_map.get(order_type_str, f"ORDER_TYPE_{order_type_str}")
        order_type_const = getattr(mt5, mapped, None)
        if order_type_const is None:
            raise TradeExecutionError(
                f"Type d'ordre différé non supporté: '{order_type_str}'."
            )
        request["type"] = order_type_const

        # Trigger proposé ou fallback marché
        trig = (
            trigger_price
            if isinstance(trigger_price, (int, float)) and trigger_price > 0
            else entry_price_market
        )
        trig = round(float(trig), digits)

        # Tick pour validations
        tick = getattr(self.mt5_connector, "get_symbol_info_tick", None)
        tick = (
            tick(symbol_info.name)
            if callable(tick)
            else getattr(mt5, "symbol_info_tick", lambda s: None)(symbol_info.name)
        )
        if not tick or not hasattr(tick, "ask") or not hasattr(tick, "bid"):
            raise TradeExecutionError(f"Tick invalide pour {symbol_info.name}.")
        ask = float(getattr(tick, "ask") or 0.0)
        bid = float(getattr(tick, "bid") or 0.0)
        if not (ask > 0 and bid > 0 and ask > bid):
            raise TradeExecutionError(
                f"Prix marché invalides (ask/bid) pour {symbol_info.name}."
            )

        # Règles MT5: distances min par type, côté BID/ASK
        too_close = (
            (
                order_type_str == "BUY_LIMIT"
                and not (trig < ask - min_stop_distance_price)
            )
            or (
                order_type_str == "SELL_LIMIT"
                and not (trig > bid + min_stop_distance_price)
            )
            or (
                order_type_str == "BUY_STOP"
                and not (trig > ask + min_stop_distance_price)
            )
            or (
                order_type_str == "SELL_STOP"
                and not (trig < bid - min_stop_distance_price)
            )
        )
        if too_close:
            raise TradeExecutionError(
                f"{order_type_str}: trigger {trig:.{digits}f} trop proche du marché "
                f"(ask={ask:.{digits}f}, bid={bid:.{digits}f}, min={min_stop_distance_price:.{digits}f})."
            )

        request["price"] = trig

        # Expiration policy (n’écrase pas un FOK-like déjà en SPECIFIED)
        ORDER_TIME_DAY = getattr(mt5, "ORDER_TIME_DAY", None)
        ORDER_TIME_SPECIFIED = getattr(mt5, "ORDER_TIME_SPECIFIED", None)
        if request.get("type_time") == ORDER_TIME_GTC:
            expiration_policy = str(
                (config.get("order_expiration_policy") or {}).get("type", "GTC")
            ).upper()
            if expiration_policy == "DAY" and ORDER_TIME_DAY is not None:
                request["type_time"] = ORDER_TIME_DAY
            elif expiration_policy == "SPECIFIED" and ORDER_TIME_SPECIFIED is not None:
                request["type_time"] = ORDER_TIME_SPECIFIED
                exp_str = (config.get("order_expiration_policy") or {}).get(
                    "datetime",
                    (datetime.now(timezone.utc) + timedelta(days=1)).isoformat(),
                )
                try:
                    if isinstance(exp_str, (int, float)):
                        exp_ts = int(float(exp_str))
                    else:
                        exp_dt = datetime.fromisoformat(str(exp_str))
                        if exp_dt.tzinfo is None:
                            exp_dt = exp_dt.replace(tzinfo=timezone.utc)
                        exp_ts = int(exp_dt.timestamp())
                    request["expiration"] = exp_ts
                except Exception:
                    self.logger.error(f"Expiration invalide: {exp_str}. Fallback GTC.")
                    request["type_time"] = ORDER_TIME_GTC
    else:
        raise TradeExecutionError(f"Type d'ordre non géré: '{order_type_str}'")

    # --- Détermination du prix de référence pour les vérifs distance ---
    price_ref = float(request.get("price") or entry_price_market)

    # --- Distances min broker (SL/TP vs price_ref) ---
    if min_stop_distance_price > 0:
        if action_str == "BUY":
            if (price_ref - sl_price) < min_stop_distance_price - 1e-12:
                raise TradeExecutionError(
                    f"SL trop proche: Δ={price_ref - sl_price:.{digits}f} < min {min_stop_distance_price:.{digits}f}."
                )
            if has_tp and (tp_price - price_ref) < min_stop_distance_price - 1e-12:
                raise TradeExecutionError(
                    f"TP trop proche: Δ={tp_price - price_ref:.{digits}f} < min {min_stop_distance_price:.{digits}f}."
                )
        else:  # SELL
            if (sl_price - price_ref) < min_stop_distance_price - 1e-12:
                raise TradeExecutionError(
                    f"SL trop proche: Δ={sl_price - price_ref:.{digits}f} < min {min_stop_distance_price:.{digits}f}."
                )
            if has_tp and (price_ref - tp_price) < min_stop_distance_price - 1e-12:
                raise TradeExecutionError(
                    f"TP trop proche: Δ={price_ref - tp_price:.{digits}f} < min {min_stop_distance_price:.{digits}f}."
                )

    # --- Commentaire & tags succincts (template optionnel) ---
    comment_tpl = self.config_manager.get(
        "trading.order_comment_template", "SNIPER_X|{strategy}|{order_type}"
    )
    max_len = int(self.config_manager.get("trading.comment_max_length", 31) or 31)
    strategy_tag = str(config.get("strategy_name", "N/A"))
    rule_name = str(trade_decision.get("rule_name", "") or "")
    rr_proj = (
        trade_decision.get("meta_rr_projected")
        or trade_decision.get("rr")
        or trade_decision.get("rr_effective")
    )

    # Defaults (sécurité)
    request.setdefault(
        "action", mt5_action_deal if order_type_str == "MARKET" else mt5_action_pending
    )
    request.setdefault("type_time", ORDER_TIME_GTC)

    # Meta compliance/audit
    request["_meta_rule_name"] = rule_name
    request["_meta_action"] = action_str
    request["_meta_rr"] = rr_proj
    request["_meta_stops_level_points"] = stops_lvl_points
    request["_meta_point"] = point
    request["_compliance_flags"] = {
        "direction_ok": True,
        "stops_ok": True,
        "price_digits_ok": True,
        "order_type": order_type_str,
    }

    request["strategy_type"] = str(config.get("strategy_name", "unknown")).lower()
    request["rule_name"] = rule_name or config.get("rule_name", "")
    request["meta_rr_projected"] = rr_proj

    self.logger.debug(f"Requête MT5 construite et validée : {request}")
    return request


# --- Helpers robustes ---


def _map_symbol_for_broker(self, raw_symbol: str, market_context: dict) -> str:
    """
    Retourne le symbole broker à partir d'un mapping éventuel.
    Ne renvoie JAMAIS 'UNKNOWN' : si pas de mapping, garde raw_symbol.
    """
    symbol_map = (
        market_context.get("active_broker_account", {}).get("symbol_map", {}) or {}
    )
    broker_symbol = symbol_map.get(
        raw_symbol, raw_symbol
    )  # ✅ fallback = symbole d'origine
    if broker_symbol != raw_symbol:
        self.logger.info(f"[SYMBOL MAP] {raw_symbol} -> {broker_symbol}")
    else:
        self.logger.warning(
            f"[SYMBOL MAP] Pas de mapping pour {raw_symbol}, utilisation telle quelle."
        )
    return broker_symbol


def load_decision_package(self, decision_package: dict) -> dict:
    """
    Charge le package de décision sans validation.
    """
    self.logger.debug("Chargement du package de décision (aucune validation)...")

    # Aucune validation, on retourne direct le package
    return decision_package
