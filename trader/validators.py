# trader/validators.py - Module de Validation pour le Bot SNIPER_X
from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple
from datetime import datetime, timedelta, UTC


# ======================================================================================
# Fenêtre horaire / Spread / Exposition / Pré-checks / Fat-finger / Override manuel
# (inchangés fonctionnellement, nettoyés trailing, typés, docs précises)
# ======================================================================================


def _check_trading_window(
    self, current_time_utc: datetime, symbol: str
) -> tuple[bool, str]:
    """
    Vérifie si le trading est autorisé pour l'actif donné à l'heure actuelle.
    Gère correctement les sessions de nuit (passant par minuit).
    """
    start_hour = self.config_manager.get(
        "trade_executor_settings.trading_start_hour_utc", 7
    )
    end_hour = self.config_manager.get(
        "trade_executor_settings.trading_end_hour_utc", 20
    )
    allowed_weekdays = self.config_manager.get(
        "trade_executor_settings.allowed_weekdays", [0, 1, 2, 3, 4]
    )

    if current_time_utc.weekday() not in allowed_weekdays:
        return (
            False,
            f"Hors des jours de trading autorisés (Jour: {current_time_utc.weekday()}).",
        )

    # Session normale (ex: 07:00 -> 20:00)
    if start_hour <= end_hour:
        if not (start_hour <= current_time_utc.hour < end_hour):
            return (
                False,
                f"Hors de la fenêtre de trading (Heure UTC: {current_time_utc.hour}).",
            )
    # Session de nuit (ex: 22:00 -> 07:00)
    else:
        if not (
            current_time_utc.hour >= start_hour or current_time_utc.hour < end_hour
        ):
            return (
                False,
                f"Hors de la fenêtre de trading de nuit (Heure UTC: {current_time_utc.hour}).",
            )

    return True, "Dans la fenêtre de trading."


def _check_spread(self, symbol: str, active_config: dict) -> tuple[bool, str]:
    """
    Double garde-fou sur le spread :
      1) limite absolue (points)
      2) limite dynamique via moyenne de session ( (high+low)/2 * multiplier )
    """
    try:
        info = self.mt5_connector.get_symbol_info(symbol)
        if not info:
            return False, f"Infos symbole indisponibles pour {symbol}."
        # conversions sûres
        cur = float(getattr(info, "spread", 0) or 0)
        sh = float(getattr(info, "spread_high", 0) or 0)
        sl = float(getattr(info, "spread_low", 0) or 0)
    except Exception as e:
        self.logger.warning(f"[SPREAD] get_symbol_info KO {symbol}: {e}", exc_info=True)
        return False, f"Impossible d'obtenir le spread pour {symbol}."

    # (1) Limite absolue
    max_abs = float(
        self.config_manager.get("trade_executor_settings.max_allowed_spread_points", 50)
        or 50
    )
    if cur > max_abs:
        return False, f"Spread {cur:.1f} > max absolu {max_abs:.1f}."

    # (2) Limite dynamique
    smart = (
        self.config_manager.get("trade_executor_settings.smart_spread_check", {}) or {}
    )
    if bool(smart.get("enabled", True)) and (sh > 0 and sl > 0):
        avg_session = (sh + sl) / 2.0
        mult = float(smart.get("max_multiplier", 2.5) or 2.5)
        dyn_lim = avg_session * mult
        if cur > dyn_lim:
            return False, (
                f"Spread {cur:.1f} anormal vs moyenne session {avg_session:.1f} "
                f"(limite dyn {dyn_lim:.1f})."
            )

    return True, "Spread acceptable."


def _check_portfolio_exposure(
    self, active_config: dict, current_context: dict, trade_decision: dict
) -> tuple[bool, str]:
    """
    Valide l'exposition portefeuille en RISQUE ($) — pas notionnel.
    - compte le risque du NOUVEAU trade (risk_per_trade_percent sur EQUITY)
    - + le risque des positions ouvertes (via order_calc_profit(entry→SL))
    """
    # (0) Limite nb positions ouvertes
    max_positions = int(
        self.config_manager.get("global_safety.max_open_positions", 5) or 5
    )
    open_pos_count = len(getattr(self, "_open_positions", {}) or {})
    if open_pos_count >= max_positions:
        return (
            False,
            f"Max positions ouvertes atteint ({open_pos_count}/{max_positions}).",
        )

    # (1) Equity
    equity = float(
        ((current_context or {}).get("account_info") or {}).get("equity", 0.0) or 0.0
    )
    if equity <= 0:
        return False, "Équité du compte invalide."

    # (2) Risque nouveau trade
    risk_pct = float((active_config or {}).get("risk_per_trade_percent", 0.0) or 0.0)
    if risk_pct <= 0:
        return False, "risk_per_trade_percent absent ou invalide."
    risk_new_usd = equity * (risk_pct / 100.0)

    # (3) Risque cumulé des positions ouvertes (via MT5 si dispo)
    total_existing_usd = 0.0
    mt5 = getattr(getattr(self, "mt5_connector", None), "mt5", None) or getattr(
        self, "mt5", None
    )

    def _as_float(x, d=None):
        try:
            v = float(x)
            return v if v == v else d  # NaN guard
        except Exception:
            return d

    for tkt, pos in (getattr(self, "_open_positions", {}) or {}).items():
        try:
            sym = str(pos.get("symbol") or "").upper()
            vol = _as_float(pos.get("volume"), 0.0)
            entry = _as_float(pos.get("entry_price"), None)
            sl = _as_float(pos.get("sl"), None)
            ptype = int(pos.get("type", 0) or 0)  # 0=BUY, 1=SELL (POSITION_TYPE)

            if not sym or not vol or entry is None or sl is None:
                continue
            if not mt5:
                # Fallback approx: valeur 1 point ~ contract_size, on prend |entry-sl|
                si = None
                try:
                    si = self.mt5_connector.get_symbol_info(sym)
                except Exception:
                    pass
                contract = _as_float(
                    getattr(si, "trade_contract_size", 100.0) if si else 100.0, 100.0
                )
                loss_per_lot = abs(entry - sl) * contract
                total_existing_usd += abs(loss_per_lot * vol)
                continue

            # MT5 officiel : order_calc_profit(type, symbol, volume, price_open, price_close)
            otype = (
                getattr(mt5, "ORDER_TYPE_BUY", 0)
                if ptype == getattr(self, "POSITION_TYPE_BUY", 0)
                else getattr(mt5, "ORDER_TYPE_SELL", 1)
            )
            profit = mt5.order_calc_profit(
                otype, sym, float(vol), float(entry), float(sl)
            )
            if isinstance(profit, (list, tuple)):
                profit = profit[-1]
            loss_val = _as_float(profit, 0.0)
            total_existing_usd += abs(loss_val or 0.0)
        except Exception as e:
            self.logger.warning(f"[EXPO] calc risque position #{tkt} KO: {e}")

    # (4) Seuil global
    max_total_pct = float(
        self.config_manager.get("global_safety.max_total_risk_percent", 10.0) or 10.0
    )
    max_total_usd = equity * (max_total_pct / 100.0)
    total_potential = total_existing_usd + risk_new_usd

    if total_potential > max_total_usd:
        return False, (
            f"Risque total {total_potential:.2f}$ > plafond {max_total_usd:.2f}$ "
            f"(existant {total_existing_usd:.2f}$ + nouveau {risk_new_usd:.2f}$)."
        )

    self.logger.debug(
        f"[EXPO] OK total={total_potential:.2f}$ (exist={total_existing_usd:.2f}$, new={risk_new_usd:.2f}$) "
        f"limite={max_total_usd:.2f}$"
    )
    return True, "Exposition portefeuille OK."


def pre_trade_checks(
    self,
    trade_decision: dict,
    active_config: dict,
    market_context: dict,
) -> tuple[bool, str]:
    """
    Pré-checks d’exécution (garde-fous non-stratégiques) :
      - action & symbole valides + whitelist
      - mapping broker + connexion MT5 + symbole visible/sélectionné
      - fenêtre horaire & jours (sauf CLOSE)
      - limites positions (globale + par symbole si conf)
      - prix courant côté action
      - diag spread (non bloquant)
      - SL minimal vs stops_level + cap scalping si demandé
    """
    # --- import DIAG (neutre si absent) ---
    try:
        from core.diagnostics import get_tracker_from_context
    except Exception:
        get_tracker_from_context = None

    # Helpers
    def _first_non_empty(*vals):
        for v in vals:
            if isinstance(v, str) and v.strip():
                return v.strip()
        return None

    def _normalize_action(a: str) -> str:
        a = (a or "").strip().upper()
        return {
            "BUY": "BUY",
            "SELL": "SELL",
            "LONG": "BUY",
            "SHORT": "SELL",
            "CLOSE": "CLOSE",
        }.get(a, "")

    def _mt5_is_connected() -> bool:
        attr = getattr(self.mt5_connector, "is_connected", None)
        try:
            return bool(attr()) if callable(attr) else bool(attr)
        except Exception:
            return False

    def _mt5_reconnect_if_needed():
        recon = getattr(self.mt5_connector, "reconnect_if_needed", None)
        if callable(recon):
            try:
                recon()
            except Exception:
                pass

    def _select_symbol_if_needed(sym: str) -> bool:
        try:
            sel = getattr(self.mt5_connector, "ensure_symbol_selected", None)
            if callable(sel):
                return bool(sel(sym))
            info = self.mt5_connector.get_symbol_info(sym)
            if info and getattr(info, "visible", True):
                return True
            subscribe = getattr(self.mt5_connector, "symbol_select", None)
            return bool(subscribe(sym, True)) if callable(subscribe) else True
        except Exception:
            return False

    def _diag(reason: str, extra: dict | None = None, sym: str | None = None):
        try:
            if get_tracker_from_context:
                s = sym or trade_decision.get("asset") or "UNKNOWN"
                get_tracker_from_context(market_context).note(
                    s, "pre_trade", reason, extra or {}
                )
        except Exception:
            pass

    def _reject(
        reason: str, extra: dict | None = None, sym: str | None = None
    ) -> tuple[bool, str]:
        _diag(reason, extra, sym)
        return False, reason

    # 1) Action & symbole
    action_raw = _first_non_empty(
        trade_decision.get("final_action"),
        trade_decision.get("selected_action"),
        trade_decision.get("core_action"),
        trade_decision.get("action"),
        trade_decision.get("side"),
        trade_decision.get("direction"),
    )
    action = _normalize_action(action_raw)
    if not action:
        return _reject(f"invalid_action:{action_raw or 'EMPTY'}")

    raw_symbol = _first_non_empty(
        trade_decision.get("asset"),
        trade_decision.get("symbol"),
        trade_decision.get("instrument"),
    )
    if not raw_symbol or raw_symbol.strip().upper() == "UNKNOWN":
        return _reject("asset_missing_or_unknown")
    raw_symbol = raw_symbol.strip().upper()

    # 2) Whitelist
    allowed = set(map(str.upper, (active_config or {}).get("tradeable_assets", [])))
    if allowed and raw_symbol not in allowed:
        return _reject(f"asset_not_allowed:{raw_symbol}", sym=raw_symbol)

    # 3) Mapping broker
    broker_symbol = self._map_symbol_for_broker(raw_symbol, market_context)
    if not broker_symbol or broker_symbol.strip().upper() == "UNKNOWN":
        return _reject(f"invalid_broker_mapping:{raw_symbol}", sym=raw_symbol)
    broker_symbol = broker_symbol.strip().upper()

    # 4) Connexion MT5
    if not _mt5_is_connected():
        _mt5_reconnect_if_needed()
        if not _mt5_is_connected():
            return _reject("mt5_not_connected", sym=raw_symbol)

    # 5) Symbole MT5 & sélection
    symbol_info = self.mt5_connector.get_symbol_info(broker_symbol)
    if not symbol_info or not getattr(symbol_info, "name", None):
        return _reject(f"invalid_mt5_symbol:{broker_symbol}", sym=raw_symbol)
    if not _select_symbol_if_needed(broker_symbol):
        return _reject(f"symbol_not_selected:{broker_symbol}", sym=raw_symbol)

    # 5bis) Cooldown post-exit (hard skip)
    try:
        import time as _t

        until = (getattr(self, "_cooldown_until", {}) or {}).get(broker_symbol.upper())
        if until and _t.time() < until:
            remain = int(until - _t.time())
            try:
                self.logger.info(f"[COOLDOWN] {broker_symbol} bloqué {remain}s → skip.")
            except Exception:
                pass
            return _reject(f"cooldown_after_exit_active:{remain}s", sym=raw_symbol)
    except Exception as _e:
        try:
            self.logger.warning(f"[COOLDOWN] check KO: {_e}")
        except Exception:
            pass

    # 6) Fenêtre/Calendrier (CLOSE bypass)
    tes = self.config_manager.get("trade_executor_settings", {}) or {}
    start_h = int(tes.get("trading_start_hour_utc", 0) or 0)
    end_h = int(tes.get("trading_end_hour_utc", 24) or 24)
    allowed_wd = set(tes.get("allowed_weekdays", list(range(7))) or list(range(7)))

    now_utc = datetime.now(UTC)
    if action != "CLOSE":
        if now_utc.weekday() not in allowed_wd:
            return _reject(
                f"trading_day_not_allowed:weekday={now_utc.weekday()}", sym=raw_symbol
            )
        if start_h <= end_h:
            if not (start_h <= now_utc.hour < end_h):
                return _reject(
                    f"trading_time_blocked:{start_h:02d}-{end_h:02d}Z", sym=raw_symbol
                )
        else:
            # fenêtre nocturne (ex: 22→07)
            if not (now_utc.hour >= start_h or now_utc.hour < end_h):
                return _reject(
                    f"trading_time_blocked:{start_h:02d}-{end_h:02d}Z", sym=raw_symbol
                )

    # 7) Limites positions
    active_acc = (market_context or {}).get("active_broker_account", {}) or {}
    max_pos_global = int(
        ((active_acc.get("trade_settings") or {}).get("max_open_positions", 999)) or 999
    )
    current_positions = (market_context or {}).get("open_positions", []) or []
    if (
        isinstance(current_positions, (list, tuple))
        and len(current_positions) >= max_pos_global
    ):
        return _reject(
            f"max_positions_reached:{len(current_positions)}/{max_pos_global}",
            sym=raw_symbol,
        )

    mpps = (active_acc.get("trade_settings") or {}).get("max_open_positions_per_symbol")
    if isinstance(mpps, (int, float)):
        by_sym = sum(
            1
            for p in current_positions
            if str(p.get("symbol", "")).upper() == broker_symbol
        )
        if by_sym >= int(mpps):
            return _reject(
                f"max_positions_symbol_reached:{broker_symbol}:{by_sym}/{int(mpps)}",
                sym=raw_symbol,
            )

    # 8) Prix courant côté action
    price = self.mt5_connector.get_current_price(broker_symbol, action)
    if not price or price <= 0:
        return _reject("price_unavailable", sym=raw_symbol)

    # 9) Diag spread (non bloquant)
    try:
        exec_policy = (active_config or {}).get("execution_policy", {}) or {}
        max_spread_points = exec_policy.get("max_spread_points")
        if (
            hasattr(symbol_info, "spread")
            and hasattr(symbol_info, "point")
            and isinstance(max_spread_points, (int, float))
        ):
            _diag(
                "spread_points_info",
                {
                    "spread": float(symbol_info.spread),
                    "limit": float(max_spread_points),
                },
                raw_symbol,
            )
    except Exception:
        pass

    # 10) SL vs stops_level + cap scalping optionnel
    target_sl_pips = float(trade_decision.get("target_sl_pips", 0) or 0.0)
    is_scalping = "scalping" in str(trade_decision.get("strategy_type", "")).lower()

    sl_cap = float(
        self.config_manager.get("entry_rules.scalping.max_stop_pips_scalp", 0.0) or 0.0
    )
    reject_over_cap = bool(
        self.config_manager.get("entry_rules.scalping.reject_if_sl_over_cap", False)
    )
    if is_scalping and sl_cap > 0 and target_sl_pips > sl_cap and reject_over_cap:
        return _reject(
            f"sl_over_cap({target_sl_pips:.2f}>{sl_cap:.2f})",
            {"sl_pips": target_sl_pips, "cap": sl_cap},
            raw_symbol,
        )

    try:
        digits = int(getattr(symbol_info, "digits", 5) or 5)
        points_per_pip = 10.0 if digits in (3, 5) else 1.0
    except Exception:
        points_per_pip = 10.0
    stops_level_points = float(
        getattr(
            symbol_info, "trade_stops_level", getattr(symbol_info, "stops_level", 0)
        )
        or 0
    )
    stops_level_pips = (
        (stops_level_points / points_per_pip) if points_per_pip > 0 else 0.0
    )

    if is_scalping and target_sl_pips > 0 and stops_level_pips > target_sl_pips:
        return _reject(
            "stops_level_too_high_for_scalp",
            {"stops_level_pips": stops_level_pips, "sl_pips": target_sl_pips},
            raw_symbol,
        )

    return True, ""


def _check_fat_finger_volume(
    self, trade_decision: dict, market_context: dict
) -> tuple[bool, str]:
    """
    Vérifie si le volume calculé est anormalement élevé (fat-finger check) en utilisant
    une limite absolue et une limite dynamique par rapport à la moyenne récente.
    """
    symbol = trade_decision.get("asset")
    proposed_volume = trade_decision.get("volume", 0.0)

    settings = self.config_manager.get("trade_executor_settings.fat_finger_check", {})

    # --- Vérification 1: Limite Absolue (Garde-fou) ---
    max_absolute_volume = settings.get("max_absolute_volume_for_asset", {})
    if symbol in max_absolute_volume and proposed_volume > max_absolute_volume[symbol]:
        return (
            False,
            f"Volume ({proposed_volume:.2f}) dépasse le seuil absolu ({max_absolute_volume[symbol]:.2f}) pour {symbol}.",
        )

    # --- AMÉLIORATION : Vérification 2: Limite Dynamique (Intelligente) ---
    if settings.get("enable_dynamic_check", True):
        # Accéder de manière robuste au DataFrame d'analyse
        market_data_for_asset = market_context.get("market_data", {}).get(symbol, {})
        rates_df = market_data_for_asset.get("annotated_rates_df")

        if rates_df is None or "tick_volume" not in rates_df.columns:
            self.logger.warning(
                f"Données de volume manquantes pour {symbol}. Vérification dynamique du volume ignorée."
            )
            return True, "Volume acceptable (vérification dynamique ignorée)."

        lookback = settings.get("avg_volume_lookback", 20)
        if len(rates_df) >= lookback:
            # Calcul optimisé de la moyenne avec pandas
            avg_recent_volume = (
                rates_df["tick_volume"].rolling(window=lookback).mean().iloc[-1]
            )

            # Éviter la division par zéro si le volume moyen est nul
            if avg_recent_volume > 1e-9:
                multiplier = settings.get("max_volume_multiplier_from_avg", 5.0)
                dynamic_limit = avg_recent_volume * multiplier

                if proposed_volume > dynamic_limit:
                    reason = (
                        f"Volume ({proposed_volume:.2f}) est > {multiplier}x la moyenne récente "
                        f"({avg_recent_volume:.2f}). Limite dynamique: {dynamic_limit:.2f}."
                    )
                    return False, reason

    return True, "Volume de trade acceptable (fat-finger check)."


def manual_override_if_needed(self, mt5_request: dict) -> bool:
    """
    Déclenche le workflow Human-in-the-Loop de façon non bloquante.
    - Retourne True  => exécution immédiate (pas d’override requis)
    - Retourne False => ordre placé en attente d’approbation manuelle
    """
    # 0) Feature flag
    if not bool(
        self.config_manager.get(
            "trade_executor_settings.manual_override_enabled", False
        )
    ):
        self.logger.debug("Override manuel désactivé → exécution directe.")
        return True

    # 1) Seuil de volume
    try:
        volume = float(mt5_request.get("volume", 0.0) or 0.0)
    except Exception:
        volume = 0.0
    try:
        threshold = float(
            self.config_manager.get(
                "trade_executor_settings.manual_override_volume_threshold", 1.0
            )
            or 1.0
        )
    except Exception:
        threshold = 1.0

    if volume < threshold:
        self.logger.debug(
            f"Volume {volume:.2f} < seuil {threshold:.2f} → pas d’override."
        )
        return True

    # 2) Init mémoire pending
    if not hasattr(self, "_pending_orders") or not isinstance(
        self._pending_orders, dict
    ):
        self._pending_orders = {}

    # 3) Order ID robuste
    symbol = str(
        mt5_request.get("symbol") or mt5_request.get("asset") or "UNKNOWN"
    ).upper()
    order_id = str(
        mt5_request.get("order_id")
        or mt5_request.get("client_order_id")
        or f"MAN_{symbol}_{int(datetime.now(UTC).timestamp())}"
    )

    # 4) Idempotence : déjà en attente ?
    if order_id in self._pending_orders and str(
        self._pending_orders[order_id].get("status")
    ).startswith("pending"):
        self.logger.warning(
            f"[MANUAL] Ordre {order_id} déjà en attente → aucun changement."
        )
        return False

    # 5) Timeout
    try:
        timeout_secs = int(
            self.config_manager.get(
                "trade_executor_settings.manual_override_timeout_seconds", 60
            )
            or 60
        )
    except Exception:
        timeout_secs = 60
    expires_at = datetime.now(UTC) + timedelta(seconds=max(1, timeout_secs))

    # 6) Enregistrement pending
    self._pending_orders[order_id] = {
        "request": dict(mt5_request),
        "status": "pending_manual_approval",
        "symbol": symbol,
        "volume": volume,
        "timestamp_requested": datetime.now(UTC).isoformat(),
        "timeout_utc": expires_at.isoformat(),
        "reason": f"Volume élevé: {volume:.2f} lots.",
    }
    self.logger.warning(
        f"[MANUAL] Contrôle requis pour ordre {order_id} ({symbol}, {volume:.2f} lots) → en attente."
    )

    # 7) Notification opérateur (best-effort)
    try:
        self.config_manager.request_manual_override(
            reason=f"Volume élevé ({volume:.2f} lots) pour {symbol}",
            trade_decision=mt5_request,
            context={"volume": volume, "threshold": threshold, "order_id": order_id},
        )
    except Exception as e:
        self.logger.error(f"[MANUAL] Envoi notification KO: {e}", exc_info=True)

    # 8) Bloque l'exécution immédiate
    return False


def approve_pending_order(
    self, order_id: str, user: str, action: str = "APPROVE"
) -> dict:
    """
    Traite un ordre en attente (APPROVE/REJECT).
    """
    action_u = str(action or "").strip().upper()
    self.logger.info(
        f"[MANUAL] Action '{action_u}' sur ordre '{order_id}' par '{user}'."
    )

    if not hasattr(self, "_pending_orders") or not isinstance(
        self._pending_orders, dict
    ):
        self._pending_orders = {}

    if order_id not in self._pending_orders:
        self.logger.warning(f"[MANUAL] Ordre inconnu ou déjà traité: {order_id}")
        return {
            "status": "error",
            "message": "Order ID not found in pending list or already processed.",
        }

    # Lecture sans supprimer d’abord (pour pouvoir remettre si action invalide)
    order_data = self._pending_orders.get(order_id) or {}
    request_snapshot = order_data.get("request") or {}

    # Timeout expiré ?
    try:
        exp = order_data.get("timeout_utc")
        if exp and datetime.now(UTC) > datetime.fromisoformat(exp):
            # Expiration → rejet auto
            self._pending_orders.pop(order_id, None)
            self.logger.warning(
                f"[MANUAL] Timeout expiré pour {order_id} → rejet auto."
            )
            try:
                self.config_manager.log_manual_intervention(
                    user,
                    self.config_manager.ManualAction.TRADE_REJECTED.value,
                    {"order_id": order_id, "reason": "timeout_expired"},
                )
            except Exception:
                pass
            return {
                "status": "rejected",
                "message": f"Order {order_id} expired (timeout).",
            }
    except Exception:
        pass

    if action_u == "APPROVE":
        # Retire de la file et exécute
        self._pending_orders.pop(order_id, None)
        self.logger.info(f"[MANUAL] APPROVE {order_id} → envoi exécution.")
        try:
            self.config_manager.log_manual_intervention(
                user,
                self.config_manager.ManualAction.TRADE_APPROVED.value,
                {"order_id": order_id, "request_snapshot": request_snapshot},
            )
        except Exception:
            pass
        try:
            return self.execute_order(request_snapshot)
        except Exception as e:
            self.logger.error(
                f"[MANUAL] Exécution KO pour {order_id}: {e}", exc_info=True
            )
            return {
                "status": "error",
                "message": f"Execution failed for {order_id}: {e}",
            }

    elif action_u == "REJECT":
        self._pending_orders.pop(order_id, None)
        self.logger.info(f"[MANUAL] REJECT {order_id} par '{user}'.")
        try:
            self.config_manager.log_manual_intervention(
                user,
                self.config_manager.ManualAction.TRADE_REJECTED.value,
                {"order_id": order_id, "request_snapshot": request_snapshot},
            )
        except Exception:
            pass
        # NB: si un ordre broker différé avait déjà été envoyé, l’annulation broker est à gérer ailleurs.
        return {"status": "rejected", "message": f"Order {order_id} manually rejected."}

    else:
        self.logger.warning(
            f"[MANUAL] Action inconnue '{action}' pour {order_id} → aucun changement."
        )
        return {"status": "error", "message": "Invalid action for pending order."}


# ======================================================================================
# VALIDATIONS DE CONFIG — SL/TP & BURST (sans trailing) + rétro-compat douce
# ======================================================================================


def _get_in(d: dict, path: str, default=None):
    """
    Accès nested avec chemin 'a.b.c'. Renvoie default si la clé n'existe pas.
    """
    cur = d or {}
    for part in path.split("."):
        if isinstance(cur, dict) and part in cur:
            cur = cur[part]
        else:
            return default
    return cur


def _is_number(x) -> bool:
    try:
        return isinstance(x, (int, float)) and (x == x)
    except Exception:
        return False


def validate_burst_and_sltp_config(self, config: dict) -> dict:
    """
    Valide la config scalping/burst (SL/TP + guardrails) sans échouer sur anciens champs trailing.
    Retour:
      {
        "ok": bool,
        "errors": [str],
        "warnings": [str],
      }
    """
    errors: List[str] = []
    warnings: List[str] = []

    # --- Nœuds utiles ---
    bs = _get_in(config, "entry_rules.scalping.burst_scalping", {}) or {}
    sltp = bs.get("sltp", {}) or {}
    closure = bs.get("closure_rules", {}) or {}

    # === Burst size ===
    bs_val = bs.get("burst_size")
    if bs_val is not None:
        try:
            n = int(bs_val)
            if n < 1:
                errors.append(
                    "entry_rules.scalping.burst_scalping.burst_size must be >= 1"
                )
        except Exception:
            errors.append(
                "entry_rules.scalping.burst_scalping.burst_size must be an integer"
            )

    # === SLTP (RR dynamique + méthode SL) ===
    rr_base = sltp.get("rr_base", 1.5)
    rr_floor = sltp.get("rr_floor", 1.0)
    rr_cap = sltp.get("rr_cap", 3.0)
    for key, val, cond, msg in [
        (
            "rr_base",
            rr_base,
            (_is_number(rr_base) and rr_base > 0),
            "sltp.rr_base must be > 0",
        ),
        (
            "rr_floor",
            rr_floor,
            (_is_number(rr_floor) and rr_floor >= 0),
            "sltp.rr_floor must be >= 0",
        ),
        (
            "rr_cap",
            rr_cap,
            (_is_number(rr_cap) and rr_cap >= rr_floor),
            "sltp.rr_cap must be >= rr_floor",
        ),
    ]:
        if not cond:
            errors.append(f"entry_rules.scalping.burst_scalping.sltp.{msg}")

    sl_method = str(sltp.get("sl_method", "PIPS") or "PIPS").upper()
    if sl_method not in {"PIPS", "ATR", "SWING"}:
        errors.append(
            "entry_rules.scalping.burst_scalping.sltp.sl_method must be one of: PIPS, ATR, SWING"
        )

    # === Closure rules ===
    def _must_bool(path: str):
        v = _get_in(bs, f"closure_rules.{path}", None)
        if v is None:
            return
        if not isinstance(v, bool):
            errors.append(
                f"entry_rules.scalping.burst_scalping.closure_rules.{path} must be boolean"
            )

    def _must_num_ge(path: str, ge: float):
        v = _get_in(bs, f"closure_rules.{path}", None)
        if v is None:
            return
        if not _is_number(v) or float(v) < ge:
            errors.append(
                f"entry_rules.scalping.burst_scalping.closure_rules.{path} must be >= {ge}"
            )

    def _must_int_ge(path: str, ge: int):
        v = _get_in(bs, f"closure_rules.{path}", None)
        if v is None:
            return
        try:
            iv = int(v)
            if iv < ge:
                errors.append(
                    f"entry_rules.scalping.burst_scalping.closure_rules.{path} must be >= {ge}"
                )
        except Exception:
            errors.append(
                f"entry_rules.scalping.burst_scalping.closure_rules.{path} must be integer >= {ge}"
            )

    _must_bool("close_on_full_profit")
    _must_bool("require_full_count_for_profit_close")
    _must_bool("require_all_seen_green_once")
    _must_num_ge("all_seen_green_pips", 0.0)
    _must_num_ge("min_green_pnl_pips", 0.0)
    _must_int_ge("loss_guard_arming_ms", 0)
    _must_int_ge("rt_fast_window_ms", 0)
    _must_int_ge("rt_poll_interval_ms", 10)
    _must_num_ge("max_loss_pips", 0.00001)
    _must_num_ge("cooldown_after_exit_s", 0.0)

    # === Cooldown (emplacement alternatif global accepté) ===
    cd_glob = _get_in(config, "cooldown_after_exit_s", None)
    cd_local = _get_in(bs, "closure_rules.cooldown_after_exit_s", None)
    if (cd_glob is None) and (cd_local is None):
        warnings.append(
            "No cooldown_after_exit_s provided (either root or entry_rules.scalping.burst_scalping.closure_rules)."
        )

    # === Dépréciations: trailing / exit_rules single_master historiques ===
    deprecated_paths = [
        "entry_rules.scalping.trailing",
        "exit_rules.trailing",
        "exit_rules.single_master",
        "smart_trailing_settings",
        "smart_sl_tp_settings.trailing_enabled",
    ]
    for p in deprecated_paths:
        if _get_in(config, p, None) is not None:
            warnings.append(
                f"Deprecated config detected: '{p}' (ignored in SL/TP burst mode)."
            )

    # === Résultat ===
    ok = len(errors) == 0
    if ok and not warnings:
        self.logger.debug("[VALIDATORS] SL/TP & Burst config: OK.")
    elif ok and warnings:
        self.logger.warning(
            f"[VALIDATORS] SL/TP & Burst config: OK with warnings: {warnings}"
        )
    else:
        self.logger.error(f"[VALIDATORS] SL/TP & Burst config: ERRORS: {errors}")

    return {"ok": ok, "errors": errors, "warnings": warnings}


def validate_liquidity_config(self, config: dict) -> dict:
    """
    Validation légère pour la stratégie liquidity (rien de spécifique trailing).
    Cible: présence de la liste d'actifs, risk_per_trade_percent > 0, quelques clés d'entry.
    """
    errors: List[str] = []
    warnings: List[str] = []

    assets = config.get("tradeable_assets", [])
    if not isinstance(assets, list) or not assets:
        errors.append("tradeable_assets must be a non-empty list")

    r = config.get("risk_per_trade_percent", None)
    if not _is_number(r) or float(r) <= 0:
        errors.append("risk_per_trade_percent must be > 0")

    # Exemple de présence minimale de blocs attendus
    entry_rules = config.get("entry_rules", {})
    if not isinstance(entry_rules, dict) or not entry_rules:
        warnings.append("entry_rules not provided (using defaults may degrade results)")

    return {"ok": not errors, "errors": errors, "warnings": warnings}


def validate_config(self, strategy_name: str, config: dict) -> dict:
    """
    Point d’entrée générique pour valider une config de stratégie.
    - Scalping: contrôle SL/TP & Burst (sans trailing)
    - Liquidity: contrôle minimal (risque/actifs)
    """
    strat = (strategy_name or "").strip().lower()
    if strat == "scalping":
        return validate_burst_and_sltp_config(self, config)
    if strat == "liquidity":
        return validate_liquidity_config(self, config)
    # Default: pas de blocage, mais avertissement
    self.logger.warning(
        f"[VALIDATORS] Unknown strategy '{strategy_name}', no strict validation applied."
    )
    return {"ok": True, "errors": [], "warnings": ["no_strict_validation_for_strategy"]}
