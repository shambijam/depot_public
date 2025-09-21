#sniper_patterns/structure_detector.py

import pandas as pd
from typing import Dict, Any

def detect_structure(df: pd.DataFrame, i: int) -> Dict[str, Any]:
    """
    Contexte structurel : OB, FVG, BOS, supports/résistances...
    """
    
    return {
        "near_ob": "ob_zone" in df.columns and not pd.isna(df["ob_zone"].iloc[i]),
        "near_fvg": "fvg" in df.columns and not pd.isna(df["fvg"].iloc[i]),
        "near_bos": "bos" in df.columns and not pd.isna(df["bos"].iloc[i]),
    }
