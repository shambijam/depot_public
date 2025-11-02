# phase_observer/detect_orderflow_v6/result_builder.py
from __future__ import annotations
from typing import Dict, Any, List


def build_result(
    score: float,
    status: str,
    summary: Dict[str, Any],
    patterns: Dict[str, Any] | list,
    vp: Dict[str, Any],
) -> Dict[str, Any]:
    """
    Agrège le résultat final au format institutionnel.
    - `summary.volume_profile` contient l'intégralité des métriques VP (VPOC, VA, HVN/LVN, modality, balance_metrics, ib, ...).
    - Compat V5 : alias directs dans `summary` pour `vpoc_price`, `va_low`, `va_high`.
    - `patterns` : accepte dict de flags ou liste d'événements (comme V5).
      * Si liste d'événements: on compte les types uniques (clé 'pattern') pour éviter la sur-pondération.
      * Si dict: on compte les flags True.
    """

    # -------- Normalisation du score / statut --------
    try:
        s = float(score)
    except Exception:
        s = 0.0
    s = max(0.0, min(100.0, s))

    st = str(status).upper()
    st = "VALID" if st == "VALID" else "SUSPECT"

    # -------- Normalisation des patterns + comptage --------
    pat = (
        patterns if patterns is not None else ([] if isinstance(patterns, list) else {})
    )
    pattern_count = 0

    if isinstance(pat, dict):
        try:
            pattern_count = int(sum(1 for v in pat.values() if bool(v)))
        except Exception:
            pattern_count = 0
    elif isinstance(pat, list):
        # liste d'événements : compter les types uniques si possible
        try:
            uniq_types = {
                (ev.get("pattern") or "").strip() for ev in pat if isinstance(ev, dict)
            }
            uniq_types.discard("")
            pattern_count = len(uniq_types) if uniq_types else len(pat)
        except Exception:
            pattern_count = len(pat)
    else:
        # format inattendu → neutralisation
        pat = {}
        pattern_count = 0

    # -------- Volume Profile (encapsulation propre) --------
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
    if "nodes" in vp:
        vp_slim["nodes"] = vp["nodes"]

    # -------- Merge summary + alias V5 + bloc volume_profile --------
    merged_summary: Dict[str, Any] = dict(summary or {})
    merged_summary.setdefault("pattern_count", pattern_count)

    # Aliases V5-friendly
    merged_summary["vpoc_price"] = vp_slim.get("vpoc_price")
    merged_summary["va_low"] = vp_slim.get("va_low")
    merged_summary["va_high"] = vp_slim.get("va_high")

    # Bloc structuré complet
    merged_summary["volume_profile"] = vp_slim

    # -------- Résultat final --------
    final = {
        "score": s,
        "status": st,
        "summary": merged_summary,
        "patterns": pat,
    }
    return final
