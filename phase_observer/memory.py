# phase_observer/memory.py
from __future__ import annotations

import logging
from datetime import datetime
from typing import Any, Dict, List, Optional

from .types import PhaseSignal, PhaseMemory, PhaseSnapshot, Direction

logger = logging.getLogger(__name__)

__all__ = ["stability_filter", "update_memory", "reset_memory"]


class PhaseMemoryManager:
    """Gère la mémoire des phases pour chaque actif."""

    def __init__(self, config_manager=None, logger=None):
        self.config_manager = config_manager
        self.logger = logger or logging.getLogger(__name__)
        self._last_phases: Dict[str, str] = {}
        self._phase_counters: Dict[str, Dict[str, Any]] = {}

    def get_last_phase(self, asset_symbol: str) -> Optional[str]:
        """Retourne la dernière phase connue pour un actif, ou None si inconnu."""
        return self._last_phases.get(asset_symbol)

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
                if (str(last_bias) == "BUY" and str(sig.direction) == "SELL") or (
                    str(last_bias) == "SELL" and str(sig.direction) == "BUY"
                ):
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

        # Maj mémoire
        memory.caches["persist_counts"] = persist_counts
        memory.caches["ema_quality"] = ema_quality
        memory.last_update = datetime.utcnow()
        return adjusted

    def get_memory(self, asset_symbol: str) -> PhaseMemory:
        """Retourne (ou initialise) la mémoire de phase pour un actif."""
        if asset_symbol not in self._phase_counters:
            self._phase_counters[asset_symbol] = PhaseMemory()
        return self._phase_counters[asset_symbol]

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

        # Sécurité : s'assurer que recent_signals est bien une liste
        if not isinstance(memory.recent_signals, list):
            memory.recent_signals = []

        # Ajouter les nouveaux signaux
        if new_signals:
            memory.recent_signals.extend(new_signals)
        if len(memory.recent_signals) > keep_last:
            memory.recent_signals = memory.recent_signals[-keep_last:]

        # Gérer snapshot et transitions
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

        # Toujours mettre à jour la phase courante
        memory.last_phase = new_phase
        memory.last_update = datetime.utcnow()
        self._last_phases[asset_symbol] = new_phase

        # 🔥 BONUS : reset compteur de persistance (cohérent avec apply_phase_memory)
        if asset_symbol in self._phase_counters:
            self._phase_counters[asset_symbol] = {"candidate": None, "count": 0}

        return memory



    def apply_phase_memory(
        self, asset_symbol: str, current_phase: str, confidence: float
    ) -> str:
        """
        📌 Stabilisation de phase via mémoire améliorée
        - Si current_phase == "no_clear_phase" → on garde la dernière phase
        - Seuil de confiance dynamique (configurable, défaut = 0.55)
        - Persistance : une phase candidate doit apparaître plusieurs fois avant d'être validée
        """
        try:
            memory = self.get_memory(asset_symbol)
            last_phase = memory.last_phase

            # Charger la config si dispo
            threshold = 0.55
            persistence_required = 2
            if getattr(self, "config_manager", None):
                try:
                    threshold = float(
                        self.config_manager.get(
                            "phase_detection_defaults.memory.min_confidence_threshold",
                            threshold,
                        )
                    )
                    persistence_required = int(
                        self.config_manager.get(
                            "phase_detection_defaults.memory.persistence_cycles",
                            persistence_required,
                        )
                    )
                except Exception as e:
                    self.logger.warning(f"[Memory] config_manager get failed: {e}")

            # Cas 1: pas de phase précédente → init
            if not last_phase:
                self.update_memory(asset_symbol, current_phase)
                return current_phase

            # Cas 2: pas clair ou faible confiance → conserver la précédente
            if current_phase == "no_clear_phase" or confidence < threshold:
                return last_phase

            # 🔥 Initialiser le compteur pour l’actif si manquant
            if asset_symbol not in self._phase_counters:
                self._phase_counters[asset_symbol] = {"candidate": None, "count": 0}

            counters = self._phase_counters[asset_symbol]

            # Cas 3: la phase candidate est identique à la dernière → reset
            if current_phase == last_phase:
                counters["candidate"] = None
                counters["count"] = 0
                return last_phase

            # Cas 4: candidate différente → incrémentation du compteur
            if counters["candidate"] == current_phase:
                counters["count"] += 1
            else:
                counters["candidate"] = current_phase
                counters["count"] = 1

            # Valider transition seulement après persistance_required cycles
            if counters["count"] >= persistence_required:
                self.logger.info(
                    f"[Memory] ✅ Transition confirmée: {last_phase} → {current_phase} "
                    f"(confiance={confidence:.2f}, persistance={counters['count']})"
                )
                self.update_memory(asset_symbol, current_phase)
                self._phase_counters[asset_symbol] = {"candidate": None, "count": 0}
                return current_phase
            else:
                self.logger.debug(
                    f"[Memory] ⏳ Transition en attente: {last_phase} → {current_phase} "
                    f"(confiance={confidence:.2f}, tentative {counters['count']}/{persistence_required})"
                )
                return last_phase

        except Exception as e:
            self.logger.error(f"[Memory] apply_phase_memory failed: {e}", exc_info=True)
            return current_phase


    def reset_memory(memory: PhaseMemory) -> None:
        """Purge la mémoire en douceur (sans recréer l’objet)."""
        try:
            memory.recent_signals.clear()
            memory.caches.clear()
            memory.last_snapshot = None
            memory.last_update = None
        except Exception:
            logger.debug(
                "reset_memory: nettoyage partiel (forme inattendue de memory)."
            )
