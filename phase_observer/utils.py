#  phase_observer/utils.py
# --- MUST BE FIRST LINE ---
from __future__ import annotations

import numpy as np
import pandas as pd
import json
from pathlib import Path
from typing import Optional, Dict, Any, Tuple, List


def _extract_m1_break_direction(self, analyzed_m1_row: pd.Series) -> Tuple[Optional[str], bool]:
    """Retourne ('BUY'|'SELL'|None, break_ok) via détails BOS/MSS M1 (+ volume)."""
    try:
        bos = analyzed_m1_row.get("bos_mss_details")
        if isinstance(bos, dict):
            t = str(bos.get("type", "")).lower()
            vol_ok = float(bos.get("volume_ratio", 0) or 0.0) > 1.5
            if "bullish" in t:
                return "BUY", vol_ok
            if "bearish" in t:
                return "SELL", vol_ok
        return None, False
    except Exception:
        return None, False
    
def _pick_sl_from_structure(self, analyzed_m1_row: pd.Series, side: str) -> Optional[float]:
    """SL = bord d’OB cohérent sinon dernier swing opposé. Retourne None si impossible."""
    try:
        if not side:
            return None

        ob = analyzed_m1_row.get("ob_details")
        if isinstance(ob, dict):
            zone = ob.get("zone")
            if isinstance(zone, (list, tuple)) and len(zone) == 2:
                low, high = zone
                if side == "BUY" and isinstance(low, (int, float)):
                    return float(low)
                if side == "SELL" and isinstance(high, (int, float)):
                    return float(high)

        # Fallback swings
        if side == "BUY" and pd.notna(analyzed_m1_row.get("last_swing_low")):
            return float(analyzed_m1_row["last_swing_low"])
        if side == "SELL" and pd.notna(analyzed_m1_row.get("last_swing_high")):
            return float(analyzed_m1_row["last_swing_high"])
        return None
    except Exception:
        return None
    
def _pick_tp_from_nearest_liquidity(self, analyzed_df: pd.DataFrame, side: str) -> Optional[float]:
    """TP = niveau de liquidité le plus proche dans le sens du trade. Toujours non bloquant."""
    try:
        if analyzed_df is None or analyzed_df.empty or side not in {"BUY", "SELL"}:
            return None

        liq = self._get_nearest_liquidity_level(analyzed_df) if hasattr(self, "_get_nearest_liquidity_level") else None
        if not liq:
            return None

        level = liq.get("level")
        last_close = float(analyzed_df["close"].iloc[-1])

        # Choix du candidat
        if isinstance(level, (list, tuple)) and len(level) == 2:
            candidate = max(level) if side == "BUY" else min(level)
        else:
            try:
                candidate = float(level)
            except Exception:
                return None

        # S’assurer que le TP est bien “dans le bon sens”
        if side == "BUY" and candidate <= last_close:
            candidate = last_close + abs(last_close - candidate)
        if side == "SELL" and candidate >= last_close:
            candidate = last_close - abs(last_close - candidate)

        # Petit buffer optionnel (évite TP collé au prix)
        point_value = (
            analyzed_df.get("point", pd.Series([0.00001])).iloc[-1]
            if "point" in analyzed_df.columns else self.config_manager.get(
                "phase_detection_defaults.default_point_value", 0.00001
            )
        )
        try:
            pv = float(point_value) if float(point_value) > 0 else 0.00001
        except Exception:
            pv = 0.00001

        buffer = 5 * pv
        candidate = float(candidate + buffer if side == "BUY" else candidate - buffer)

        return float(candidate)
    except Exception:
        return None
    
def _get_nearest_liquidity_level(self, df: pd.DataFrame) -> Optional[Dict[str, Any]]:
    """
    Détermine le niveau de liquidité (EQH/EQL, OB, FVG) le plus proche de la dernière barre
    et calcule la distance en pips.
    """
    self.logger.debug("Détection du niveau de liquidité le plus proche...")

    if df is None or df.empty:
        self.logger.debug("DataFrame vide pour la détection du niveau de liquidité. Retourne None.")
        return None

    last_close = float(df["close"].iloc[-1])

    point_value = (
        float(df["point"].iloc[-1])
        if "point" in df.columns and pd.notna(df["point"].iloc[-1]) and float(df["point"].iloc[-1]) > 0
        else float(self.config_manager.get("phase_detection_defaults.default_point_value", 0.00001))
    )
    if point_value <= 0:
        self.logger.warning("Valeur de 'point' invalide. Fallback 0.00001.")
        point_value = 0.00001

    nearest_level: Optional[Dict[str, Any]] = None
    min_distance_pips = np.inf

    liquidity_levels: List[Dict[str, Any]] = []

    # EQH/EQL
    if "eqh_eql_details" in df.columns:
        for eq_details in df["eqh_eql_details"].dropna():
            if isinstance(eq_details, dict) and eq_details.get("level") is not None:
                liquidity_levels.append({"type": str(eq_details.get("type", "EQ")), "level": eq_details["level"]})

    # OB
    if "ob_details" in df.columns:
        for ob_details_entry in df["ob_details"].dropna():
            if isinstance(ob_details_entry, dict) and ob_details_entry.get("zone") is not None:
                try:
                    ob_low, ob_high = ob_details_entry["zone"]
                    liquidity_levels.append({"type": "OB_low", "level": float(ob_low)})
                    liquidity_levels.append({"type": "OB_high", "level": float(ob_high)})
                except Exception:
                    continue

    # FVG
    if "fvg_details" in df.columns:
        for fvg_details_entry in df["fvg_details"].dropna():
            if isinstance(fvg_details_entry, dict) and fvg_details_entry.get("top") is not None and fvg_details_entry.get("bottom") is not None:
                try:
                    liquidity_levels.append({"type": "FVG_top", "level": float(fvg_details_entry["top"])})
                    liquidity_levels.append({"type": "FVG_bottom", "level": float(fvg_details_entry["bottom"])})
                except Exception:
                    continue

    if not liquidity_levels:
        self.logger.debug("Aucun niveau de liquidité détecté pour la dernière barre. Retourne None.")
        return None

    for liq_level in liquidity_levels:
        level_value = liq_level["level"]
        if isinstance(level_value, (int, float)):
            distance_in_price = abs(last_close - float(level_value))
        elif isinstance(level_value, (list, tuple)) and len(level_value) == 2:
            dist_to_low = abs(last_close - float(level_value[0]))
            dist_to_high = abs(last_close - float(level_value[1]))
            distance_in_price = min(dist_to_low, dist_to_high)
        else:
            self.logger.warning(f"Niveau de liquidité invalide détecté: {level_value}. Ignoré.")
            continue

        distance_pips = round(distance_in_price / point_value, 2)
        if distance_pips < min_distance_pips:
            min_distance_pips = distance_pips
            nearest_level = {"type": liq_level["type"], "level": liq_level["level"], "distance_pips": distance_pips}

    if nearest_level:
        self.logger.debug(
            f"Niveau de liquidité le plus proche : Type={nearest_level['type']}, "
            f"Level={nearest_level['level']}, Distance={nearest_level['distance_pips']:.2f} pips."
        )
    return nearest_level

def _build_enhanced_signals(self, confluence: Dict, quality: Dict, tf_analyses: Dict, asset: str) -> Dict[str, Any]:
    """
    Construction des signaux finaux enrichis (non bloquant).
    """
    try:
        # Signaux de base depuis confluence
        base_signals = {
            "phase": confluence.get("phase", "uncertain"),
            "confidence_score": float(confluence.get("confluence_score", 0.0) or 0.0),
            "is_liquid": True,  # TODO: branche ton vrai critère de liquidité ici
            "current_price": float(tf_analyses.get("M1", {}).get("last_close", 0.0) or 0.0)
                if isinstance(tf_analyses.get("M1"), dict) else 0.0,
            "asset": asset,
        }

        # Enrichissement multi-TF
        multi_tf_enhancement = {
            "multi_tf_enabled": True,
            "tf_consensus": bool(confluence.get("phase_consistency", False)),
            "dominant_tf": confluence.get("dominant_timeframe", "M5"),
            "quality_grade": quality.get("performance_grade", "C"),
            "execution_time_ms": float(quality.get("execution_time_ms", 0.0) or 0.0),
            # Signaux de confluence (booléens dérivés)
            "bos_mss_detected": (confluence.get("signal_scores", {}).get("bos_mss_detected", 0.0) or 0.0) > 0.5,
            "liquidity_grab_detected": (confluence.get("signal_scores", {}).get("liquidity_grab_detected", 0.0) or 0.0) > 0.5,
            "ob_detected": (confluence.get("signal_scores", {}).get("ob_detected", 0.0) or 0.0) > 0.5,
            # Méta-données pour debugging
            "tf_breakdown": {tf: analysis.get("phase") for tf, analysis in (tf_analyses or {}).items()},
            "signal_agreement_rates": confluence.get("agreement_rates", {}),
        }

        return {**base_signals, **multi_tf_enhancement}
    except Exception as e:
        self.logger.warning(f"_build_enhanced_signals fallback: {e}")
        return {"phase": "uncertain", "confidence_score": 0.0, "is_liquid": True, "current_price": 0.0, "asset": asset}
    
def _detect_tf_divergences(self, tf_analyses: Dict) -> Dict[str, Any]:
    """
    Détection de divergences inter-timeframes (signaux contradictoires).
    """
    divergences: List[Dict[str, Any]] = []

    bullish_tfs: List[str] = []
    bearish_tfs: List[str] = []

    for tf, analysis in (tf_analyses or {}).items():
        phase = str(analysis.get("phase", "") or "")
        if ("bullish" in phase) or ("up" in phase):
            bullish_tfs.append(tf)
        elif ("bearish" in phase) or ("down" in phase):
            bearish_tfs.append(tf)

    has_directional_conflict = (len(bullish_tfs) > 0 and len(bearish_tfs) > 0)

    if has_directional_conflict:
        divergences.append({
            "type": "directional_conflict",
            "bullish_tfs": bullish_tfs,
            "bearish_tfs": bearish_tfs,
            "severity": "high",
        })

    return {
        "detected_divergences": divergences,
        "has_conflicts": len(divergences) > 0,
        "conflict_severity": "high" if has_directional_conflict else "none",
    }
    
def _calculate_advanced_confluence(self, tf_analyses: Dict, weights: Dict, asset: str) -> Dict[str, Any]:
    """
    Algorithme de confluence sophistiqué avec scoring non-linéaire.
    Tolérant : n’échoue jamais → renvoie un paquet cohérent.
    """
    try:
        confluence_scores: Dict[str, float] = {}
        signal_agreement: Dict[str, float] = {}

        # Phases dominantes
        phases = [str(analysis.get("phase", "unknown")) for analysis in (tf_analyses or {}).values()]
        phase_consistency = (len(set(phases)) == 1 and len(phases) > 0)

        # Signaux critiques
        critical_signals = ["bos_mss_detected", "liquidity_grab_detected", "ob_detected"]

        for signal in critical_signals:
            signal_scores = []
            for tf, analysis in (tf_analyses or {}).items():
                if analysis.get(signal, False):
                    weight = float(weights.get(tf, 0.33) or 0.33)
                    signal_scores.append(weight)

            confluence_scores[signal] = float(sum(signal_scores))
            signal_agreement[signal] = (len(signal_scores) / max(1, len(tf_analyses)))

        # Base
        base_confluence = (sum(confluence_scores.values()) / max(1, len(critical_signals)))

        # Bonus cohérence de phase
        phase_bonus = 0.3 if phase_consistency else 0.0

        # Bonus d’accord moyen
        avg_agreement = (sum(signal_agreement.values()) / max(1, len(signal_agreement))) if signal_agreement else 0.0
        agreement_bonus = avg_agreement * 0.2

        final_confluence_score = float(min(1.0, base_confluence + phase_bonus + agreement_bonus))

        # Phase finale (consensus) — fallback si la méthode n’existe pas
        if hasattr(self, "_determine_consensus_phase"):
            final_phase = self._determine_consensus_phase(phases, tf_analyses, weights)
        else:
            # Fallback simple : majorité pondérée
            counter: Dict[str, float] = {}
            for tf, analysis in (tf_analyses or {}).items():
                ph = str(analysis.get("phase", "unknown"))
                counter[ph] = counter.get(ph, 0.0) + float(weights.get(tf, 0.33) or 0.33)
            final_phase = max(counter, key=counter.get) if counter else "uncertain"

        return {
            "phase": final_phase,
            "confluence_score": final_confluence_score,
            "signal_scores": confluence_scores,
            "phase_consistency": phase_consistency,
            "agreement_rates": signal_agreement,
            "dominant_timeframe": max(weights, key=weights.get) if weights else None,
        }
    except Exception as e:
        self.logger.warning(f"_calculate_advanced_confluence fallback: {e}")
        return {
            "phase": "uncertain",
            "confluence_score": 0.0,
            "signal_scores": {},
            "phase_consistency": False,
            "agreement_rates": {},
            "dominant_timeframe": None,
        }
   
  

 
 

