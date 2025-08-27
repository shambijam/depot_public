# phase_observer/memory.py
from __future__ import annotations

import logging
from datetime import datetime
from typing import Any, Dict, List, Optional

from .types import PhaseSignal, PhaseMemory, PhaseSnapshot, Direction

logger = logging.getLogger(__name__)

__all__ = ["stability_filter", "update_memory", "reset_memory"]


def stability_filter(
    signals: List[PhaseSignal],
    memory: PhaseMemory,
    *,
    min_persist_bars: int = 2,
    hysteresis: float = 0.1,
    ema_alpha: float = 0.4,
) -> List[PhaseSignal]:
    """
    Lisse et stabilise les signaux (persistance, hystérèse, EMA de qualité).
    N'élimine pas les signaux : ajuste `quality` et ajoute des métadonnées.
    """
    if not signals:
        return signals

    # Prépare les caches dans memory
    memory.caches.setdefault("persist_counts", {})
    memory.caches.setdefault("ema_quality", {})

    persist_counts: Dict[tuple, int] = memory.caches["persist_counts"]
    ema_quality: Dict[tuple, float] = memory.caches["ema_quality"]

    # Contexte pour l’hystérèse
    last_bias = getattr(memory.last_snapshot, "bias", None)
    last_phase = getattr(memory.last_snapshot, "phase", None)

    adjusted: List[PhaseSignal] = []

    for sig in signals:
        key = (sig.kind, sig.label, str(getattr(sig, "direction", "NEUTRAL")))

        # Persistance
        persist_counts[key] = int(persist_counts.get(key, 0)) + 1

        # EMA sur quality
        prev_ema = float(ema_quality.get(key, sig.quality))
        new_ema = (ema_alpha * float(sig.quality)) + ((1.0 - ema_alpha) * prev_ema)
        ema_quality[key] = new_ema

        # Hystérèse si contradiction avec le biais précédent
        q = float(sig.quality)
        stability_note: List[str] = []

        if last_bias is not None and hasattr(sig, "direction"):
            if (str(last_bias) == "BUY" and str(sig.direction) == "SELL") or \
               (str(last_bias) == "SELL" and str(sig.direction) == "BUY"):
                q = max(0.0, q - float(hysteresis))
                stability_note.append("hysteresis_penalty")

        # Renforcement si le signal a persistance suffisante
        pc = persist_counts[key]
        if pc < int(min_persist_bars):
            q *= 0.8
            stability_note.append(f"warming_up({pc}/{min_persist_bars})")
        else:
            q = (q + new_ema) * 0.5
            stability_note.append("ema_blend")

        # Mise à jour du signal (non destructif)
        sig.meta = dict(sig.meta or {})
        sig.meta.update({
            "stability_persist": pc,
            "stability_ema_quality": round(new_ema, 4),
            "stability_notes": stability_note,
            "last_phase": str(last_phase) if last_phase is not None else None,
            "last_bias": str(last_bias) if last_bias is not None else None,
        })
        sig.quality = max(0.0, min(1.0, q))
        adjusted.append(sig)

    # Maj mémoire
    memory.caches["persist_counts"] = persist_counts
    memory.caches["ema_quality"] = ema_quality
    memory.last_update = datetime.utcnow()
    return adjusted


def update_memory(
    memory: PhaseMemory,
    new_signals: List[PhaseSignal],
    snapshot: Optional[PhaseSnapshot] = None,
    *,
    keep_last: int = 200,
) -> PhaseMemory:
    """Ajoute des signaux récents et met à jour le snapshot courant (avec découpe)."""
    if not isinstance(memory.recent_signals, list):
        memory.recent_signals = []
    memory.recent_signals.extend(new_signals)
    if len(memory.recent_signals) > keep_last:
        memory.recent_signals = memory.recent_signals[-keep_last:]

    if snapshot is not None:
        memory.last_snapshot = snapshot

    memory.last_update = datetime.utcnow()
    return memory


def reset_memory(memory: PhaseMemory) -> None:
    """Purge la mémoire en douceur (sans recréer l’objet)."""
    try:
        memory.recent_signals.clear()
        memory.caches.clear()
        memory.last_snapshot = None
        memory.last_update = None
    except Exception:
        logger.debug("reset_memory: nettoyage partiel (forme inattendue de memory).")
