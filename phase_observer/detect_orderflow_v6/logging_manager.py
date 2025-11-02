# phase_observer/detect_orderflow_v6/logging_manager.py
from __future__ import annotations
import time
import json
from typing import Callable, Any, Dict, Optional, Tuple, Deque
from collections import deque
from datetime import datetime, timezone

# --- Stockages légers en mémoire (anti-doublons / rate-limit) ---
_RECENT_SIGS: Deque[Tuple[str, Tuple[Any, ...]]] = deque(maxlen=256)
_LAST_BY_KEY: Dict[str, float] = {}
_ONCE_KEYS: Dict[str, float] = {}

def _utc_iso() -> str:
    return datetime.now(timezone.utc).isoformat()

def _emit(logger, level: str, msg: str, *, extra: Optional[Dict[str, Any]] = None, structured: bool = False):
    """Émission unique, tolérante aux erreurs, structurée ou non."""
    if logger is None:
        return
    try:
        log_fn = getattr(logger, level, logger.info)
        if structured:
            payload = {"ts": _utc_iso(), "lvl": level.upper(), "msg": msg}
            if extra:
                payload.update(extra)
            log_fn(json.dumps(payload, ensure_ascii=False))
        else:
            if extra:
                # Affichage concis des extras clés
                tail = " " + " ".join(f"{k}={v}" for k, v in extra.items())
                log_fn(f"{msg}{tail}")
            else:
                log_fn(msg)
    except Exception:
        # Ne jamais casser le flux pour un problème de log
        pass

def safe_log(logger, level: str, msg: str, *, extra: Optional[Dict[str, Any]] = None, structured: bool = False,
             rate_key: Optional[str] = None, rate_seconds: float = 0.0):
    """
    Log protégé + (optionnel) rate-limit par clé.
    """
    if rate_key:
        now = time.perf_counter()
        last = _LAST_BY_KEY.get(rate_key, 0.0)
        if now - last < max(0.0, rate_seconds):
            return
        _LAST_BY_KEY[rate_key] = now
    _emit(logger, level, msg, extra=extra, structured=structured)

def log_once(logger, key: str, level: str, msg: str, *, extra: Optional[Dict[str, Any]] = None,
             structured: bool = False, ttl_seconds: float = 60.0):
    """
    Log une seule fois par 'key' pendant 'ttl_seconds'.
    """
    now = time.perf_counter()
    last = _ONCE_KEYS.get(key)
    if last is not None and (now - last) < max(0.0, ttl_seconds):
        return
    _ONCE_KEYS[key] = now
    _emit(logger, level, msg, extra=extra, structured=structured)

def log_if_changed(logger, signature: Tuple[Any, ...], level: str, msg: str, *,
                   extra: Optional[Dict[str, Any]] = None, structured: bool = False, channel: str = "default"):
    """
    N'émet que si la signature (valeurs clés) a changé récemment (anti-doublon).
    """
    sig = (channel, signature)
    if sig in _RECENT_SIGS:
        return
    _RECENT_SIGS.append(sig)
    _emit(logger, level, msg, extra=extra, structured=structured)

def timeit(logger=None, label: str = "block", *, level: str = "info", warn_ms: Optional[float] = None,
           structured: bool = False, extra: Optional[Dict[str, Any]] = None):
    """
    Décorateur de mesure de temps.
      - warn_ms: si dépassé → niveau 'warning' auto.
      - structured: émet en JSON (ts/lvl/msg + extras + dur_ms)
    """
    def deco(fn: Callable):
        def wrapper(*args, **kwargs):
            t0 = time.perf_counter()
            try:
                return fn(*args, **kwargs)
            finally:
                dt_ms = (time.perf_counter() - t0) * 1000.0
                lvl = level
                if warn_ms is not None and dt_ms >= warn_ms:
                    lvl = "warning"
                fields = dict(extra or {})
                fields["dur_ms"] = round(dt_ms, 2)
                fields["label"] = label
                _emit(logger, lvl, f"[OF V6] {label}", extra=fields, structured=structured)
        return wrapper
    return deco

class Span:
    """
    Context manager de mesure (début/fin) avec statut et timing.
    Usage:
        with Span(logger, "volume_profile", warn_ms=50):
            ...
    """
    def __init__(self, logger=None, label: str = "span", *, level: str = "info",
                 warn_ms: Optional[float] = None, structured: bool = False, extra: Optional[Dict[str, Any]] = None):
        self.logger = logger
        self.label = label
        self.level = level
        self.warn_ms = warn_ms
        self.structured = structured
        self.extra = dict(extra or {})
        self._t0 = 0.0

    def __enter__(self):
        self._t0 = time.perf_counter()
        _emit(self.logger, self.level, f"[OF V6] {self.label}.start", extra=self.extra, structured=self.structured)
        return self

    def __exit__(self, exc_type, exc, tb):
        dt_ms = (time.perf_counter() - self._t0) * 1000.0
        fields = dict(self.extra)
        fields["dur_ms"] = round(dt_ms, 2)
        fields["label"] = self.label
        if exc is not None:
            fields["error"] = str(exc)
            _emit(self.logger, "error", f"[OF V6] {self.label}.error", extra=fields, structured=self.structured)
            # Ne masque pas l'exception
            return False
        lvl = self.level
        if self.warn_ms is not None and dt_ms >= self.warn_ms:
            lvl = "warning"
        _emit(self.logger, lvl, f"[OF V6] {self.label}.end", extra=fields, structured=self.structured)
        return False

def log_exception(logger, msg: str, exc: Optional[BaseException] = None, *,
                  level: str = "error", structured: bool = False, extra: Optional[Dict[str, Any]] = None):
    """
    Helper compact pour log d'exception (sans casser la chaîne en cas d'erreur d'émission).
    """
    fields = dict(extra or {})
    if exc is not None:
        fields["error"] = str(exc)
        fields["exc_type"] = getattr(exc, "__class__", type(exc)).__name__
    _emit(logger, level, msg, extra=fields, structured=structured)
