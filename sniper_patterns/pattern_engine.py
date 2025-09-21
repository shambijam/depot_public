# sniper_patterns/pattern_engine.py

import pandas as pd
from typing import List, Dict, Any, Optional

# Briques fonctionnelles
from .candle_detector import detect_single_candle
from .multi_candle_detector import detect_multi_candle_patterns
from .combo_detector import detect_combos
from .context_enricher import enrich_context
from .structure_detector import enrich_structure
from .multi_tf_confirmer import confirm_multi_tf
from .orderflow_detector import detect_orderflow   


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

    def __init__(self, enable_context=True, enable_structure=True, enable_multi_tf=True, enable_orderflow=True):
        self.enable_context = enable_context
        self.enable_structure = enable_structure
        self.enable_multi_tf = enable_multi_tf
        self.enable_orderflow = enable_orderflow   # 🆕

    def analyze(self, df: pd.DataFrame, with_combo: bool = True) -> Dict[str, List[Optional[Dict[str, Any]]]]:
        """
        Analyse un DataFrame OHLC et retourne tous les patterns détectés.
        ------------------------------------------------
        - candle_signals  : bougies individuelles
        - multi_signals   : patterns multi-bougies
        - combo_signals   : fusion enrichie
        - orderflow_signals : lecture du flux (déséquilibre, absorption, exhaustion)
        """
        if df is None or len(df) < 5:
            return {
                "candle_signals": [],
                "multi_signals": [],
                "combo_signals": [],
                "orderflow_signals": [],
            }

        # 1️⃣ Bougies simples
        candle_signals = [detect_single_candle(df, i) for i in range(len(df))]

        # 2️⃣ Multi-bougies
        multi_signals = detect_multi_candle_patterns(df)

        # 3️⃣ Combos fusionnés
        combo_signals = detect_combos(df) if with_combo else []

        # 4️⃣ Order Flow
        orderflow_signals = detect_orderflow(df) if self.enable_orderflow else []

        # 5️⃣ Enrichissements (optionnels, activables par flags)
        if self.enable_context:
            combo_signals = enrich_context(df, combo_signals)
        if self.enable_structure:
            combo_signals = enrich_structure(df, combo_signals)
        if self.enable_multi_tf:
            combo_signals = confirm_multi_tf(df, combo_signals)

        # 6️⃣ Retour structuré pour le pipeline
        return {
            "candle_signals": candle_signals,
            "multi_signals": multi_signals,
            "combo_signals": combo_signals,
            "orderflow_signals": orderflow_signals,   # 🆕 ajouté
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
        
