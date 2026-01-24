# core/diagnostics.py
from collections import defaultdict, deque
from datetime import datetime, timezone

class NullTracker:
    def note(self, *a, **k): pass
    def set_selected(self, *a, **k): pass
    def emit_summary(self, *a, **k): pass

class DiagnosticTracker:
    """
    Collecte des motifs de rejet / étapes de décision par cycle.
    Usage :
      t = DiagnosticTracker(cycle)
      t.note("EURUSD", "pre_trade", "phase_not_directional", extra={"phase":"range"})
      t.emit_summary(logger)
    """
    def __init__(self, cycle_id: int, keep_last:int=50):
        self.cycle_id = cycle_id
        self.events = []                      # liste chronologique
        self.by_asset = defaultdict(list)     # asset -> [events]
        self.reasons_count = defaultdict(int) # reason -> count
        self.selected_asset = None
        self.start_ts = datetime.now(timezone.utc).isoformat()
        self.keep_last = keep_last
        self.last_seen = deque(maxlen=keep_last)

    def note(self, asset: str, stage: str, reason: str, extra: dict | None=None):
        evt = {
            "asset": str(asset).upper(),
            "stage": stage,
            "reason": reason,
            "extra": extra or {},
        }
        self.events.append(evt)
        self.by_asset[evt["asset"]].append(evt)
        self.reasons_count[reason] += 1
        self.last_seen.append(evt)

    def set_selected(self, asset: str, rule: str, confidence: float | None=None):
        self.selected_asset = {"asset": str(asset).upper(), "rule": rule, "confidence": confidence}

    def emit_summary(self, logger, max_assets: int = 8):
        if not logger:
            return
        sel = self.selected_asset or {}
        logger.info("── DIAG CYCLE #%s (%s) ──", self.cycle_id, self.start_ts)
        if sel:
            logger.info("🎯 sélection CORE: %s (rule=%s, conf=%s)",
                        sel.get("asset"), sel.get("rule"), sel.get("confidence"))

        # top raisons
        if self.reasons_count:
            top = sorted(self.reasons_count.items(), key=lambda x: x[1], reverse=True)[:10]
            logger.info("❌ Top motifs de refus (count): %s",
                        ", ".join([f"{r}×{c}" for r,c in top]))
        else:
            logger.info("✅ Aucun motif de refus collecté.")

        # par asset (limité)
        shown = 0
        for asset, evts in self.by_asset.items():
            if shown >= max_assets: break
            parts = [f"{e['stage']}:{e['reason']}" for e in evts[-5:]]  # derniers 5
            logger.info("· %s → %s", asset, " | ".join(parts) if parts else "OK")
            shown += 1

def get_tracker_from_context(context: dict):
    try:
        t = (context or {}).get("diag_tracker")
        return t if isinstance(t, DiagnosticTracker) else NullTracker()
    except Exception:
        return NullTracker()
