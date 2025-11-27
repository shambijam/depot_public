# phase_observer/memory.py
from __future__ import annotations

import logging
from datetime import datetime
from typing import Any, Dict, List, Optional

from .types import PhaseSignal, PhaseMemory, PhaseSnapshot, Direction

logger = logging.getLogger(__name__)

__all__ = ["PhaseMemoryManager", "reset_memory"]


class PhaseMemoryManager:
    """Gère la mémoire des phases pour chaque actif."""

    def __init__(self, config_manager=None, logger=None):
        self.config_manager = config_manager
        self.logger = logger or logging.getLogger(__name__)
        self._last_phases: Dict[str, str] = {}
        self._phase_counters: Dict[str, PhaseMemory] = {}

    def get_last_phase(self, asset_symbol: str) -> Optional[str]:
        """Retourne la dernière phase connue pour un actif, ou None si inconnu."""
        return self._last_phases.get(asset_symbol)

    def get_memory(self, asset_symbol: str) -> PhaseMemory:
        """Retourne (ou initialise) la mémoire de phase pour un actif."""
        memory = self._phase_counters.get(asset_symbol)

        if memory is None:
            memory = PhaseMemory()
            self._phase_counters[asset_symbol] = memory
            return memory

        if isinstance(memory, dict):
            try:
                memory = PhaseMemory.from_dict(memory)
                self._phase_counters[asset_symbol] = memory
                self.logger.warning(
                    f"[Memory] ⚠️ Conversion d’un dict brut vers PhaseMemory pour {asset_symbol}"
                )
            except Exception as e:
                self.logger.error(
                    f"[Memory] Impossible de convertir dict en PhaseMemory ({asset_symbol}): {e}"
                )
                memory = PhaseMemory()
                self._phase_counters[asset_symbol] = memory

        return memory

    def save_memory(
        self, asset_symbol: str, memory: Dict[str, Any] | PhaseMemory
    ) -> None:
        """Sauvegarde la mémoire pour un actif donné."""
        if isinstance(memory, dict):
            try:
                memory = PhaseMemory.from_dict(memory)
            except Exception as e:
                self.logger.error(
                    f"[Memory] save_memory: conversion dict->PhaseMemory échouée: {e}"
                )
                memory = PhaseMemory()
        self._phase_counters[asset_symbol] = memory

    def stability_filter(
        self,
        signals: List[PhaseSignal],
        memory: PhaseMemory,
        *,
        min_persist_bars: int = 2,
        hysteresis: float = 0.1,
        ema_alpha: float = 0.4,
    ) -> List[PhaseSignal]:
        """Stabilise les signaux via persistance et hystérèse."""
        if not signals:
            return signals

        memory.caches.setdefault("persist_counts", {})
        memory.caches.setdefault("ema_quality", {})

        persist_counts: Dict[tuple, int] = memory.caches["persist_counts"]
        ema_quality: Dict[tuple, float] = memory.caches["ema_quality"]

        last_bias = getattr(memory.last_snapshot, "bias", None)
        last_phase = getattr(memory.last_snapshot, "phase", None)

        adjusted: List[PhaseSignal] = []

        for sig in signals:
            key = (sig.kind, sig.label, str(getattr(sig, "direction", "NEUTRAL")))
            persist_counts[key] = int(persist_counts.get(key, 0)) + 1

            prev_ema = float(ema_quality.get(key, sig.quality))
            new_ema = (ema_alpha * float(sig.quality)) + ((1.0 - ema_alpha) * prev_ema)
            ema_quality[key] = new_ema

            q = float(sig.quality)
            stability_note: List[str] = []

            if last_bias is not None and hasattr(sig, "direction"):
                if (str(last_bias) == "BUY" and str(sig.direction) == "SELL") or (
                    str(last_bias) == "SELL" and str(sig.direction) == "BUY"
                ):
                    q = max(0.0, q - float(hysteresis))
                    stability_note.append("hysteresis_penalty")

            pc = persist_counts[key]
            if pc < int(min_persist_bars):
                q *= 0.8
                stability_note.append(f"warming_up({pc}/{min_persist_bars})")
            else:
                q = (q + new_ema) * 0.5
                stability_note.append("ema_blend")

            sig.meta = dict(sig.meta or {})
            sig.meta.update(
                {
                    "stability_persist": pc,
                    "stability_ema_quality": round(new_ema, 4),
                    "stability_notes": stability_note,
                    "last_phase": str(last_phase) if last_phase is not None else None,
                    "last_bias": str(last_bias) if last_bias is not None else None,
                }
            )
            sig.quality = max(0.0, min(1.0, q))
            adjusted.append(sig)

        memory.caches["persist_counts"] = persist_counts
        memory.caches["ema_quality"] = ema_quality
        memory.last_update = datetime.utcnow()
        return adjusted

    def update_memory(
        self,
        asset_symbol: str,
        new_phase: str,
        new_signals: Optional[List[PhaseSignal]] = None,
        snapshot: Optional[PhaseSnapshot] = None,
        *,
        keep_last: int = 200,
    ) -> PhaseMemory:
        """Met à jour la mémoire d’un actif donné."""
        memory = self.get_memory(asset_symbol)

        if not isinstance(memory.recent_signals, list):
            memory.recent_signals = []

        if new_signals:
            memory.recent_signals.extend(new_signals)
        if len(memory.recent_signals) > keep_last:
            memory.recent_signals = memory.recent_signals[-keep_last:]

        if snapshot is not None:
            old_phase = memory.last_snapshot.phase if memory.last_snapshot else None
            new_phase_snapshot = snapshot.phase if snapshot else None

            if old_phase and new_phase_snapshot and old_phase != new_phase_snapshot:
                self.logger.info(
                    f"[Memory] 📊 Phase changée: {old_phase} → {new_phase_snapshot} @ {snapshot.timestamp}"
                )
                memory.phase_transitions.append(
                    {
                        "from": old_phase,
                        "to": new_phase_snapshot,
                        "time": snapshot.timestamp,
                    }
                )
            memory.last_snapshot = snapshot

        memory.last_phase = new_phase
        memory.last_update = datetime.utcnow()
        self._last_phases[asset_symbol] = new_phase

        if not hasattr(memory, "counters") or not isinstance(memory.counters, dict):
            memory.counters = {}
        memory.counters["persistence"] = {"candidate": None, "count": 0}

        return memory

    def apply_phase_memory(
        self, asset_symbol: str, current_phase: str, confidence: float
    ) -> str:
        """Stabilisation de phase via mémoire améliorée."""
        try:
            memory = self.get_memory(asset_symbol)

            if memory is None:
                memory = PhaseMemory(
                    last_phase=current_phase,
                    confidence=confidence,
                    persistence=1,
                )
                self.save_memory(asset_symbol, memory)
                return current_phase

            last_phase = memory.last_phase

            threshold = 0.55
            persistence_required = 2
            if getattr(self, "config_manager", None):
                try:
                    threshold = float(
                        self.config_manager.get(
                            "phase_observer.memory_threshold", default=0.55
                        )
                    )
                    persistence_required = int(
                        self.config_manager.get(
                            "phase_observer.persistence_required", default=2
                        )
                    )
                except Exception as cfg_err:
                    self.logger.warning(f"[Memory] Erreur chargement config: {cfg_err}")
            # --- PATCH B2: Ajustement contextuel selon la phase candidate ---
            cp = (current_phase or "").lower()
            # En très forte volatilité : durcir (éviter les faux basculements)
            if "high_volatility" in cp:
                threshold = min(0.90, threshold + 0.10)
                persistence_required = persistence_required + 1
            # En range : assouplir légèrement (réactivité)
            elif "range_" in cp or cp == "range_retail":
                threshold = max(0.40, threshold - 0.05)

            if current_phase == last_phase:
                memory.persistence += 1
                memory.confidence = max(memory.confidence, confidence)
            else:
                if confidence < threshold or memory.persistence < persistence_required:
                    memory.persistence += 1
                    self.save_memory(asset_symbol, memory)
                    # 🔥 On reste sur la dernière phase connue
                    return memory.last_phase
                memory.last_phase = current_phase
                memory.persistence = 1
                memory.confidence = confidence

            # 🔥 Toujours garantir une phase valide
            if not memory.last_phase:
                memory.last_phase = current_phase

            self.save_memory(asset_symbol, memory)
            # ✅ GARDE-FOU FINAL : Ne jamais retourner None
            final_phase = memory.last_phase or current_phase or "range_retail"
            return final_phase

        except Exception as e:
            self.logger.error(f"[Memory] apply_phase_memory failed: {e}", exc_info=True)
            # 🔥 fallback : jamais None
            final_phase = current_phase or self.get_last_phase(asset_symbol) or "range_retail"
            return final_phase
        
    # === Gestion des Footprints (live & final) ===
    def store_footprint(
        self,
        asset_symbol: str,
        delta: float,
        poc: Optional[float],
        is_live: bool = False,
        *,
        keep_last: int = 50,
    ) -> None:
        """
        Stocke un footprint (live ou final) dans la mémoire de l'actif.
        - delta: déséquilibre acheteurs-vendeurs
        - poc: Point of Control
        - is_live: True = footprint intra-minute, False = footprint final
        """
        try:
            memory = self.get_memory(asset_symbol)
            memory.caches.setdefault("footprints", [])
            footprints: List[Dict[str, Any]] = memory.caches["footprints"]

            footprints.append(
                {
                    "time": datetime.utcnow().isoformat(),
                    "delta": float(delta) if delta is not None else 0.0,
                    "poc": float(poc) if poc is not None else None,
                    "is_live": bool(is_live),
                }
            )

            if len(footprints) > keep_last:
                memory.caches["footprints"] = footprints[-keep_last:]

            self._phase_counters[asset_symbol] = memory
            self.logger.debug(
                f"[Memory] 📝 Footprint stocké pour {asset_symbol} "
                f"(delta={delta}, poc={poc}, live={is_live})"
            )
        except Exception as e:
            self.logger.error(f"[Memory] store_footprint failed: {e}")


def reset_memory(memory: PhaseMemory) -> None:
    """Purge la mémoire en douceur (sans recréer l’objet)."""
    try:
        memory.recent_signals.clear()
        memory.caches.clear()
        memory.last_snapshot = None
        memory.last_update = None
    except Exception:
        logger.debug("reset_memory: nettoyage partiel (forme inattendue de memory).")
