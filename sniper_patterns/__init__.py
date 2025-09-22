# pattern_detection/__init__.py
"""
📦 sniper_patterns
Module central de détection de patterns (mode Desk Brut).
---------------------------------------------------------
Contient :
- Chandeliers individuels (candle_detector)
- Patterns multi-bougies (multi_candle_detector)
- Combos fusionnés (combo_detector)
- Enrichissements (context, structure, multi-TF)
- Flux d’ordres (orderflow_detector)
- Orchestrateur global (PatternEngine)
"""

from .candle_detector import detect_single_candle
from .multi_candle_detector import detect_multi_candle_patterns
from .combo_detector import detect_combos
from .context_enricher import enrich_context
from .structure_detector import enrich_structure
from .multi_tf_confirmer import confirm_multi_tf
from .orderflow_detector import detect_orderflow
from .pattern_engine import PatternEngine

__all__ = [
    "detect_single_candle",
    "detect_multi_candle_patterns",
    "detect_combos",
    "enrich_context",
    "enrich_structure",
    "confirm_multi_tf",
    "detect_orderflow",
    "PatternEngine",
]
