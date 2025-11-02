# phase_observer/detect_orderflow_v6/result_builder.py
from __future__ import annotations
from typing import Dict, Any

def build_result(
    score: float,
    status: str,
    summary: Dict[str, Any],
    patterns: Dict[str, Any] | list,
    vp: Dict[str, Any],
) -> Dict[str, Any]:
    """
    Agrège le résultat final au format institutionnel :
      - `summary.volume_profile` contient l'intégralité des métriques VP (VPOC, VA, HVN/LVN, modality, balance_metrics, ib, ...).
      - Compat V5 : alias directs dans `summary` pour `vpoc_price`, `va_low`, `va_high`.
      - Patterns : accepte dict de flags ou liste d'événements (comme V5). Calcule pattern_count si manquant.
    """
    # 1) Normalisation patterns (dict ou liste) + comptage
    pat = patterns or ([] if isinstance(patterns, list) else {})
    if isinstance(pat, dict):
        pattern_count = int(sum(1 for v in pat.values() if bool(v)))
    elif isinstance(pat, list):
        pattern_count = int(len(pat))
    else:
        pat = {}
        pattern_count = 0

    # 2) Volume Profile: on encapsule proprement tout ce qui arrive de `vp`
    vp = vp or {}
    vp_slim = {
        "vpoc_price": vp.get("vpoc_price"),
        "va_low": vp.get("va_low"),
        "va_high": vp.get("va_high"),
        "va_coverage": vp.get("va_coverage"),
        "bins_count": vp.get("bins_count"),
        "bin_width": vp.get("bin_width"),
        "price_min": vp.get("price_min"),
        "price_max": vp.get("price_max"),
        "hvn": vp.get("hvn"),
        "lvn": vp.get("lvn"),
        "modality": vp.get("modality"),
        "balance_metrics": vp.get("balance_metrics"),
        "ib": vp.get("ib"),
    }
    # Inclure nodes si fournis (peut être volumineux : OK, on respecte ce qui est passé)
    if "nodes" in vp:
        vp_slim["nodes"] = vp["nodes"]

    # 3) Merge summary avec alias compat + bloc volume_profile
    merged_summary = dict(summary or {})
    # Pattern count si absent (la brique scoring l'ajoute déjà mais on sécurise)
    merged_summary.setdefault("pattern_count", pattern_count)

    # Alias V5-friendly
    merged_summary["vpoc_price"] = vp_slim.get("vpoc_price")
    merged_summary["va_low"] = vp_slim.get("va_low")
    merged_summary["va_high"] = vp_slim.get("va_high")

    # Bloc structuré complet
    merged_summary["volume_profile"] = vp_slim

    # 4) Score / Status bornés & typés
    final = {
        "score": float(max(0.0, min(100.0, float(score)))),
        "status": "VALID" if str(status).upper() == "VALID" else "SUSPECT",
        "summary": merged_summary,
        "patterns": pat,
    }
    return final
