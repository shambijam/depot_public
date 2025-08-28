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
    Algorithme de confluence 'desk banque privée' – robuste, non-linéaire, sans double comptage.
    - Normalise les poids selon les TF réellement présents
    - Agrégation non-linéaire des signaux (1 - ∏(1 - w_i)) => rendements décroissants
    - Consensus de phase (strict + par 'bucket' bull/bear/range)
    - Consensus de biais directionnel (BUY/SELL/NEUTRAL)
    - Tolérant: n'échoue jamais → renvoie un paquet cohérent et audit-ready
    """
    try:
        # -------- Helpers --------
        def _normalize_weights(ws: Dict[str, float], keys: list[str]) -> Dict[str, float]:
            w = {k: float(ws.get(k, 0.0) or 0.0) for k in keys}
            total = sum(v for v in w.values() if v > 0)
            if total <= 0:
                n = max(1, len(keys))
                return {k: 1.0 / n for k in keys}
            return {k: (max(0.0, v) / total) for k, v in w.items()}

        def _phase_bucket(ph: str) -> str:
            s = str(ph or "unknown").lower()
            if ("bull" in s) or ("up" in s):
                return "bull"
            if ("bear" in s) or ("down" in s):
                return "bear"
            if ("range" in s) or ("side" in s) or ("consolid" in s):
                return "range"
            return "unknown"

        def _bias_from_analysis(a: Dict[str, Any]) -> str:
            # Cherche un champ directionnel standard : 'bias' | 'entry_bias' | 'direction' | 'signal_side'
            for k in ("bias", "entry_bias", "direction", "signal_side"):
                if k in a and a[k]:
                    v = str(a[k]).upper()
                    if v.startswith("B"):
                        return "BUY"
                    if v.startswith("S"):
                        return "SELL"
                    if v.startswith("N"):
                        return "NEUTRAL"
            return "NEUTRAL"

        # -------- Inputs & weight normalization --------
        tf_analyses = tf_analyses or {}
        if not tf_analyses:
            raise ValueError("empty tf_analyses")

        tf_list = list(tf_analyses.keys())
        weights = _normalize_weights(weights or {}, tf_list)

        # -------- Signals to aggregate (extensible) --------
        # Garder un noyau critique et agréger uniquement ceux présents dans au moins un TF
        base_signals = ["bos_mss_detected", "liquidity_grab_detected", "ob_detected"]
        present_signals = []
        for s in base_signals:
            if any(bool(a.get(s, False)) for a in tf_analyses.values()):
                present_signals.append(s)
        if not present_signals:
            # si rien de présent, on garde au moins un placeholder pour base_confluence = 0
            present_signals = base_signals[:1]

        # -------- Phases & buckets --------
        tf_phase_map: Dict[str, str] = {tf: str(a.get("phase", "unknown")) for tf, a in tf_analyses.items()}
        phases = list(tf_phase_map.values())
        phase_consistency_strict = (len(set(phases)) == 1 and len(phases) > 0)

        tf_bucket_map: Dict[str, str] = {tf: _phase_bucket(p) for tf, p in tf_phase_map.items()}
        # Poids par bucket
        bucket_weights: Dict[str, float] = {"bull": 0.0, "bear": 0.0, "range": 0.0, "unknown": 0.0}
        for tf, b in tf_bucket_map.items():
            bucket_weights[b] = bucket_weights.get(b, 0.0) + weights.get(tf, 0.0)
        # Accord 'large' par bucket (0..1)
        bucket_agreement = max(bucket_weights.values()) if bucket_weights else 0.0
        bucket_winner = max(bucket_weights, key=bucket_weights.get) if bucket_weights else "unknown"

        # -------- Aggregation non-linéaire des signaux --------
        signal_scores: Dict[str, float] = {}
        signal_agreement: Dict[str, float] = {}

        for signal in present_signals:
            # prob_OR = 1 - ∏(1 - w_tf) pour les TF où signal==True
            prod = 1.0
            agree_w = 0.0
            for tf, analysis in tf_analyses.items():
                if bool(analysis.get(signal, False)):
                    w = float(weights.get(tf, 0.0))
                    prod *= (1.0 - max(0.0, min(1.0, w)))
                    agree_w += w
            score = 1.0 - prod  # borné [0,1], rendements décroissants (anti double comptage)
            signal_scores[signal] = float(max(0.0, min(1.0, score)))
            signal_agreement[signal] = float(max(0.0, min(1.0, agree_w)))

        # Base confluence: moyenne des scores de signaux présents
        if signal_scores:
            base_confluence = sum(signal_scores.values()) / len(signal_scores)
        else:
            base_confluence = 0.0

        # Bonus cohérence de phase (bucket + strict)
        # - bonus 'large' selon accord par bucket (jusqu’à +0.20)
        # - petit bonus si strictement tous identiques (+0.10)
        phase_bonus = (0.20 * bucket_agreement) + (0.10 if phase_consistency_strict else 0.0)

        # -------- Consensus de BIAIS (BUY/SELL/NEUTRAL) --------
        bias_weights = {"BUY": 0.0, "SELL": 0.0, "NEUTRAL": 0.0}
        for tf, a in tf_analyses.items():
            b = _bias_from_analysis(a)
            bias_weights[b] = bias_weights.get(b, 0.0) + weights.get(tf, 0.0)

        # normaliser (déjà normalisés par TF, mais on clamp par sécurité)
        for k in list(bias_weights.keys()):
            bias_weights[k] = float(max(0.0, min(1.0, bias_weights[k])))

        # Choix biais final
        final_bias = max(bias_weights, key=bias_weights.get) if bias_weights else "NEUTRAL"
        bias_agreement = float(bias_weights.get(final_bias, 0.0))  # 0..1
        bias_bonus = 0.15 * bias_agreement  # jusqu’à +0.15

        # -------- Score final de confluence --------
        final_confluence_score = float(max(0.0, min(1.0, base_confluence + phase_bonus + bias_bonus)))

        # -------- Phase finale (consensus pondéré) --------
        if hasattr(self, "_determine_consensus_phase"):
            try:
                final_phase = self._determine_consensus_phase(phases, tf_analyses, weights)
            except Exception:
                # fallback pondéré par phase exacte
                counter: Dict[str, float] = {}
                for tf, ph in tf_phase_map.items():
                    counter[ph] = counter.get(ph, 0.0) + weights.get(tf, 0.0)
                final_phase = max(counter, key=counter.get) if counter else "uncertain"
        else:
            counter: Dict[str, float] = {}
            for tf, ph in tf_phase_map.items():
                counter[ph] = counter.get(ph, 0.0) + weights.get(tf, 0.0)
            final_phase = max(counter, key=counter.get) if counter else "uncertain"

        # TF dominant (poids max)
        dominant_tf = max(weights, key=weights.get) if weights else None

        # Supporting TFs (ceux qui soutiennent phase et biais finaux)
        supporting_phase_tfs = [tf for tf, ph in tf_phase_map.items() if ph == final_phase]
        supporting_bias_tfs  = [tf for tf, a in tf_analyses.items() if _bias_from_analysis(a) == final_bias]

        result = {
            "phase": final_phase,
            "phase_bucket": bucket_winner,
            "bias": final_bias,  # "BUY" | "SELL" | "NEUTRAL"

            "confluence_score": final_confluence_score,
            "base_confluence": float(base_confluence),
            "phase_bonus": float(round(phase_bonus, 4)),
            "bias_bonus": float(round(bias_bonus, 4)),

            "signal_scores": signal_scores,             # non-linéaires 0..1
            "agreement_rates": signal_agreement,        # pondération cumulée par signal 0..1
            "signal_agreement_rates": signal_agreement, # alias compat

            "phase_consistency": bool(phase_consistency_strict),
            "phase_bucket_agreement": float(round(bucket_agreement, 4)),

            "bias_weights": {k: float(round(v, 4)) for k, v in bias_weights.items()},
            "dominant_timeframe": dominant_tf,
            "weights_used": {k: float(round(v, 6)) for k, v in weights.items()},
            "supporting_tfs": {
                "phase": supporting_phase_tfs or None,
                "bias": supporting_bias_tfs or None,
            },
        }
        return result

    except Exception as e:
        self.logger.warning(f"_calculate_advanced_confluence fallback: {e}")
        return {
            "phase": "uncertain",
            "phase_bucket": "unknown",
            "bias": "NEUTRAL",
            "confluence_score": 0.0,
            "base_confluence": 0.0,
            "phase_bonus": 0.0,
            "bias_bonus": 0.0,
            "signal_scores": {},
            "agreement_rates": {},
            "signal_agreement_rates": {},
            "phase_consistency": False,
            "phase_bucket_agreement": 0.0,
            "bias_weights": {"BUY": 0.0, "SELL": 0.0, "NEUTRAL": 1.0},
            "dominant_timeframe": None,
            "weights_used": {},
            "supporting_tfs": {"phase": None, "bias": None},
        }

  

 
 

