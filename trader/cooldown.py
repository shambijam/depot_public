#trader/cooldown.py - Module de Gestion du Cooldown pour le Bot SNIPER_X
from __future__ import annotations

def _cooldown_guard(
    self,
    asset: str,
    now_ts: float,
    *,
    per_asset_cooldown_s: float = 20.0,
    min_gap_any_trade_s: float = 5.0,
    max_new_trades_per_cycle: int = 1,
) -> bool:
    """
    Retourne True si on DOIT SKIP l’envoi d’un nouvel ordre (throttle/cooldown).
    - Strictement non-bloquant : en cas d’erreur → False (on ne bloque pas l’exé par défaut).
    - Ne persiste rien au disque : mémoires RAM (attributs sur self).
    """
    try:
        # --- Normalisation entrées ---
        asset_key = str(asset or "").upper().strip()
        now_ts = float(now_ts or 0.0)

        per_asset = max(0.0, float(per_asset_cooldown_s or 0.0))
        min_gap = max(0.0, float(min_gap_any_trade_s or 0.0))
        max_per_cycle = max(0, int(max_new_trades_per_cycle or 0))

        # --- Mémoires RAM idempotentes ---
        lt_by_asset = getattr(self, "_last_trade_ts_by_asset", None)
        if not isinstance(lt_by_asset, dict):
            lt_by_asset = {}
            self._last_trade_ts_by_asset = lt_by_asset

        if not hasattr(self, "_cycle_new_trades"):
            self._cycle_new_trades = 0
        if not hasattr(self, "_last_any_trade_ts"):
            self._last_any_trade_ts = 0.0

        # 1) Limite par cycle (si >0)
        if max_per_cycle > 0 and self._cycle_new_trades >= max_per_cycle:
            self.logger.info(f"[THROTTLE] Limite par cycle atteinte ({max_per_cycle}).")
            return True

        # 2) Gap global
        last_any = float(self._last_any_trade_ts or 0.0)
        if last_any > 0.0 and (now_ts - last_any) < min_gap:
            gap = min_gap - (now_ts - last_any)
            if gap > 0:
                self.logger.info(f"[THROTTLE] Gap global actif ~{gap:.1f}s.")
                return True

        # 3) Cooldown par asset (si asset précisé)
        if asset_key:
            last_ts = float(lt_by_asset.get(asset_key, 0.0) or 0.0)
            if last_ts > 0.0 and (now_ts - last_ts) < per_asset:
                gap = per_asset - (now_ts - last_ts)
                if gap > 0:
                    self.logger.info(f"[THROTTLE] Cooldown {asset_key} encore ~{gap:.1f}s.")
                    return True

        return False

    except Exception as e:
        # Tolérance : on log et on n’empêche pas l’exécution
        try:
            self.logger.warning(f"[THROTTLE] Guard erreur (ignore): {e}")
        except Exception:
            pass
        return False

def _cooldown_on_success(self, asset: str, now_ts: float) -> None:
    """
    À appeler UNIQUEMENT après un envoi d'ordre réussi.
    Met à jour les compteurs utilisés par _cooldown_guard (global, par asset, par cycle).
    """
    try:
        asset_key = str(asset or "").upper().strip()
        now_ts = float(now_ts or 0.0)

        if not hasattr(self, "_last_trade_ts_by_asset") or not isinstance(self._last_trade_ts_by_asset, dict):
            self._last_trade_ts_by_asset = {}

        self._last_any_trade_ts = now_ts
        if asset_key:
            self._last_trade_ts_by_asset[asset_key] = now_ts
        self._cycle_new_trades = int(getattr(self, "_cycle_new_trades", 0)) + 1
    except Exception as e:
        try:
            self.logger.warning(f"[THROTTLE] Maj compteurs KO (ignore): {e}")
        except Exception:
            pass

def _cooldown_cycle_reset(self) -> None:
    """
    À appeler au début de chaque 'cycle' d'évaluation (ta boucle de scan).
    Remet à zéro le compteur de nouveaux ordres du cycle.
    """
    try:
        self._cycle_new_trades = 0
    except Exception:
        pass
