# sniper_patterns/pattern_engine.py

import pandas as pd
import json
import logging
from pathlib import Path
from typing import List, Dict, Any, Optional

# Briques fonctionnelles
from .candle_detector import detect_single_candle
from .multi_candle_detector import detect_multi_candle_patterns
from .combo_detector import detect_combos
from .context_enricher import enrich_context
from .structure_detector import enrich_structure
from .multi_tf_confirmer import confirm_multi_tf
from .orderflow_detector import detect_orderflow   

LOG = logging.getLogger(__name__)


class PatternEngine:
    """
    🏛️ PatternEngine (mode Dev Desk)
    ------------------------------------------------
    Orchestrateur unique de lecture du marché :
    - Chandeliers simples (individuels)
    - Patterns multi-bougies
    - Combinaisons enrichies (confluences, MTF, OB/FVG/BOS)
    - Order Flow (déséquilibre, absorption, exhaustion)
    - Ajout contextuel : phase, volume, volatilité

    Retourne un tableau consolidé de signaux exploitables
    pour le pipeline décisionnel du bot.
    """

    def __init__(
        self,
        enable_context=True,
        enable_structure=True,
        enable_multi_tf=True,
        enable_orderflow=True,
        patterns_file: Optional[str] = "config/sniper_patterns.json",
        auto_reload: bool = False,
    ):
        self.enable_context = enable_context
        self.enable_structure = enable_structure
        self.enable_multi_tf = enable_multi_tf
        self.enable_orderflow = enable_orderflow

        self.patterns_file = Path(patterns_file) if patterns_file else None
        self.auto_reload = auto_reload
        self._patterns_mtime = None
        self.patterns = self._load_patterns()
 

    def _load_patterns(self) -> Dict[str, Any]:
        """Charge le JSON des patterns (safe). Retourne {} si erreur."""
        if not self.patterns_file:
            return {}
        try:
            if not self.patterns_file.exists():
                LOG.warning("Patterns file introuvable: %s", self.patterns_file)
                return {}
            # auto-reload check: si modifié, relire
            if self.auto_reload:
                mtime = self.patterns_file.stat().st_mtime
                if self._patterns_mtime and mtime == self._patterns_mtime:
                    return self.patterns or {}
                self._patterns_mtime = mtime

            with self.patterns_file.open("r", encoding="utf-8") as f:
                data = json.load(f)
            LOG.info("Patterns JSON chargé depuis %s", self.patterns_file)
            return data or {}
        except Exception as e:
            LOG.exception("Erreur chargement patterns JSON: %s", e)
            return {}

    def analyze(self, df: pd.DataFrame, with_combo: bool = True) -> Dict[str, List[Optional[Dict[str, Any]]]]:
        if df is None or len(df) < 5:
            return {
                "candle_signals": [],
                "multi_signals": [],
                "combo_signals": [],
                "orderflow_signals": [],
            }

        # 1️⃣ Bougies simples (on passe patterns)
        candle_signals = [detect_single_candle(df, i, patterns=self.patterns) for i in range(len(df))]

        # 2️⃣ Multi-bougies
        multi_signals = detect_multi_candle_patterns(df, patterns=self.patterns)

        # 3️⃣ Combos fusionnés
        combo_signals = detect_combos(df, patterns=self.patterns) if with_combo else []

        # 4️⃣ Order Flow
        orderflow_signals = detect_orderflow(df, patterns=self.patterns) if self.enable_orderflow else []

        # 5️⃣ Enrichissements (optionnels, activables par flags)
        if self.enable_context:
            combo_signals = enrich_context(df, combo_signals)
        if self.enable_structure:
            combo_signals = enrich_structure(df, combo_signals)
        if self.enable_multi_tf:
            combo_signals = confirm_multi_tf(df, combo_signals)

        return {
            "candle_signals": candle_signals,
            "multi_signals": multi_signals,
            "combo_signals": combo_signals,
            "orderflow_signals": orderflow_signals,
        }


    def latest_signal(self, df: pd.DataFrame, prefer_combo: bool = True, prefer_orderflow: bool = False) -> Optional[Dict[str, Any]]:
        """
        Retourne le dernier signal pertinent (desk mode).
        Priorité : combo > orderflow > multi > candle
        """
        results = self.analyze(df, with_combo=prefer_combo)

        if prefer_combo and results["combo_signals"]:
            return results["combo_signals"][-1]
        if prefer_orderflow and results["orderflow_signals"]:
            return results["orderflow_signals"][-1]
        if results["multi_signals"]:
            return results["multi_signals"][-1]
        if results["candle_signals"]:
            return results["candle_signals"][-1]
        return None

    def trace_pipeline(self, df: pd.DataFrame) -> None:
        """
        Trace détaillée (mode desk) du pipeline.
        """
        results = self.analyze(df, with_combo=True)

        print("============================================================")
        print("🔍 TRACE PATTERN ENGINE (mode Dev Desk)")
        for i, sig in enumerate(results["combo_signals"]):
            if sig:
                print(
                    f"[{i}] {sig.get('timestamp','?')} | "
                    f"{sig.get('pattern','-')} | "
                    f"type={sig.get('signal_type','?')} | "
                    f"bullish={sig.get('is_bullish','?')} | "
                    f"OB={sig.get('near_ob',False)} FVG={sig.get('near_fvg',False)} BOS={sig.get('near_bos',False)} | "
                    f"TF_conf={sig.get('confirmed_tf',[])}"
                )
        if results["orderflow_signals"]:
            print("--- ORDERFLOW ---")
            for i, sig in enumerate(results["orderflow_signals"]):
                if sig:
                    print(
                        f"[{i}] {sig.get('timestamp','?')} | "
                        f"{sig.get('orderflow_pattern','-')} | "
                        f"imbalance={sig.get('imbalance_pct','?')} | "
                        f"dominance={sig.get('dominance','?')}"
                    )
        print("============================================================")
        
