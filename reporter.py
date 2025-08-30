# -*- coding: utf-8 -*-
"""
PhaseObserver Reporter (bank-desk grade)
---------------------------------------
Génère un rapport JOURS complet des détections:
- OB / FVG / BOS-MSS (détails horodatés, niveaux, qualité, scores)
- Régimes de marché (distribution + transitions)
- Bollinger microphase (squeeze/expansion, range_score, mid_entry, distances)
- Métriques d'hygiène (barres, trous, NaN, latence)
Export: Markdown lisible par humain OU JSONL (1 ligne = 1 événement)
"""

from __future__ import annotations
from dataclasses import dataclass, asdict
from typing import Dict, Any, List, Optional, Iterable, Tuple
from datetime import datetime, timezone, timedelta
import json
import os
import math
import logging
import pandas as pd

from phase_observer.detectors import Detectors



# ---------- Data Records (structures claires pour JSONL) ----------


@dataclass
class OBRecord:
    ts: str
    symbol: str
    timeframe: str
    kind: str  # bullish/bearish
    zone_high: float
    zone_low: float
    quality: Optional[str] = None
    score: Optional[float] = None
    meta: Optional[dict] = None


@dataclass
class FVGRecord:
    ts: str
    symbol: str
    timeframe: str
    direction: str  # up/down
    gap_top: float
    gap_bottom: float
    quality: Optional[str] = None
    score: Optional[float] = None
    meta: Optional[dict] = None


@dataclass
class BOSRecord:
    ts: str
    symbol: str
    timeframe: str
    type: str  # bullish_bos/bearish_bos/bullish_mss/bearish_mss
    level_broken: float
    confirmation_score: Optional[float] = None
    volume_ratio: Optional[float] = None
    momentum: Optional[float] = None
    structure_type: Optional[str] = None
    quality: Optional[str] = None
    meta: Optional[dict] = None


@dataclass
class RegimeSnapshot:
    ts: str
    symbol: str
    timeframe: str
    regime: str


@dataclass
class BollRecord:
    ts: str
    symbol: str
    timeframe: str
    signal: str  # buy_revert / sell_revert / buy_breakout / sell_breakout / neutral
    is_range: bool
    is_squeeze: bool
    is_expansion: bool
    range_score: float
    bb_mid: float
    bb_upper: float
    bb_lower: float
    dist_to_mid_pips: Optional[float] = None
    dist_to_upper_pips: Optional[float] = None
    dist_to_lower_pips: Optional[float] = None
    mid_entry: Optional[str] = None  # buy/sell/None
    mid_entry_score: Optional[float] = None
    entry_gate_ok: Optional[bool] = None
    half_band_pips: Optional[float] = None
    z_band: Optional[float] = None
    atr_pips: Optional[float] = None
    meta: Optional[dict] = None


@dataclass
class BigReversalRecord:
    ts: str
    symbol: str
    timeframe: str
    type: str  # big_reversal_bullish / big_reversal_bearish
    body_ratio: float
    candle_size: float
    avg_size: float
    near_ob: bool
    near_fvg: bool
    near_bos: bool
    meta: Optional[dict] = None


class PhaseObserverReporter:
    """
    Reporter instanciable (un logger + config_manager en option).
    - collect() transforme des DataFrames (par TF) en événements typés
    - aggregate() produit des stats macro
    - export_markdown() / export_jsonl() écrivent le rapport
    """
        

    def _collect_big_reversal(self, symbol: str, tf: str, df: pd.DataFrame) -> List[BigReversalRecord]:
        out: List[BigReversalRecord] = []
        try:
            detections = self.detectors.detect_big_reversal_candle(df) or []
            for d in detections:
                if not d:
                    continue
                out.append(BigReversalRecord(
                    ts=str(d.get("timestamp")),
                    symbol=symbol,
                    timeframe=tf,
                    type=d.get("type", "big_reversal_unknown"),
                    body_ratio=d.get("body_ratio", 0.0),
                    candle_size=d.get("candle_size", 0.0),
                    avg_size=d.get("avg_size", 0.0),
                    near_ob=bool(d.get("near_ob")),
                    near_fvg=bool(d.get("near_fvg")),
                    near_bos=bool(d.get("near_bos")),
                    meta={k: v for k, v in d.items() if k not in {
                        "timestamp","type","body_ratio","candle_size","avg_size","near_ob","near_fvg","near_bos"
                    }}
                ))
        except Exception as e:
            self._swallow("BigReversal", symbol, tf, e)
        return out

   

    def __init__(self, config_manager=None, logger=None):

        self.logger = logger or logging.getLogger(__name__)
        self.config_manager = config_manager
        self.detectors = Detectors(logger=self.logger, config_manager=config_manager)

    # --------------- Public API ---------------
    def run_daily_report_for_asset(
            self,
            symbol: str,
            tf_frames: Dict[str, pd.DataFrame],
            tz: timezone | None = timezone.utc,
            day_offset: int = 0,   # <--- nouveau paramètre
        ) -> Dict[str, Any]:
            """
            Construit un rapport complet pour un symbole sur plusieurs TF (ex: {"M1":df1, "M5":df5, ...})
            Retourne un dict structuré {meta, stats, events}
            day_offset: 0 = aujourd'hui, -1 = hier, -2 = avant-hier, etc.
            """
            events: List[dict] = []
            macro = {"ob": 0, "fvg": 0, "bos": 0, "regime_snapshots": 0, "boll_events": 0}
            regime_hist: List[RegimeSnapshot] = []

            for tf, df in (tf_frames or {}).items():
                if not isinstance(df, pd.DataFrame) or df.empty:
                    self.logger.warning(f"[{symbol}] TF {tf} vide/absent pour le reporter.")
                    continue

                # 1) Hygiène
                hy = self._compute_hygiene(df)
                # 2) OB/FVG/BOS/MSS
                ob_evts = self._collect_ob(symbol, tf, df)
                macro["ob"] += len(ob_evts)
                fvg_evts = self._collect_fvg(symbol, tf, df)
                macro["fvg"] += len(fvg_evts)
                bos_evts = self._collect_bos(symbol, tf, df)
                macro["bos"] += len(bos_evts)
                
                # 2.5) Grandes bougies de retournement
                big_reversal_evts = self._collect_big_reversal(symbol, tf, df)
                macro["big_reversal"] = macro.get("big_reversal", 0) + len(big_reversal_evts)
               

                # 3) Régime
                regime_series = self._collect_regime_series(symbol, tf, df)
                macro["regime_snapshots"] += len(regime_series)
                regime_hist.extend(regime_series)

                # 4) Bollinger microphase (dernière barre + meta série si demandée)
                boll_evt = self._collect_boll_micro(symbol, tf, df)
                macro["boll_events"] += 1 if boll_evt else 0

                # 5) Convert to dicts + merge
                events.extend([asdict(x) for x in ob_evts])
                events.extend([asdict(x) for x in fvg_evts])
                events.extend([asdict(x) for x in bos_evts])
                events.extend([asdict(x) for x in big_reversal_evts])
                events.extend([asdict(x) for x in regime_series])
                if boll_evt:
                    events.append(asdict(boll_evt))

                # Hygiène en tant que meta-event minimal (facilite audit)
                events.append(
                    {
                        "ts": self._last_ts_iso(df),
                        "symbol": symbol,
                        "timeframe": tf,
                        "type": "hygiene",
                        "bars": hy["bars"],
                        "nan_ratio": hy["nan_ratio"],
                        "holes": hy["holes"],
                        "start": hy["start"],
                        "end": hy["end"],
                    }
                )

            # Agrégations macro: distribution régimes + transitions
            regime_stats, regime_transitions = self._aggregate_regimes(regime_hist)
            stats = {
                "counts": macro,
                "regimes": regime_stats,
                "regime_transitions": regime_transitions,
            }

            # ✅ correction ici : date peut être décalée avec day_offset
            target_date = datetime.now(timezone.utc).date() + timedelta(days=day_offset)

            meta = {
                "symbol": symbol,
                "timeframes": list(tf_frames.keys()),
                "date_utc": str(target_date),
                "generated_at": datetime.now(timezone.utc).isoformat(),
                "version": "1.0",
            }
            return {"meta": meta, "stats": stats, "events": events}

    def export_markdown(self, report: Dict[str, Any], path: str) -> None:
        """Écrit un rapport humain-lisible (Markdown)"""
        md = self._render_markdown(report)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            f.write(md)
        self.logger.info(f"[Reporter] Markdown écrit: {path}")

    def export_jsonl(self, report: Dict[str, Any], path: str) -> None:
        """Écrit un JSONL: 1 événement par ligne (+ header/stats en 1ère/2e lignes)"""
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            f.write(
                json.dumps({"_header": report.get("meta", {})}, ensure_ascii=False)
                + "\n"
            )
            f.write(
                json.dumps({"_stats": report.get("stats", {})}, ensure_ascii=False)
                + "\n"
            )
            for evt in report.get("events", []):
                f.write(json.dumps(evt, ensure_ascii=False) + "\n")
        self.logger.info(f"[Reporter] JSONL écrit: {path}")

    # --------------- Collectors (évènements) ---------------

    def _collect_ob(self, symbol: str, tf: str, df: pd.DataFrame) -> List[OBRecord]:
        out: List[OBRecord] = []
        try:
            detections = self.detectors.detect_order_block_ml_enhanced(df) or []
            for d in detections:
                if not d:
                    continue
                ts = self._match_ts(df, d)
                out.append(
                    OBRecord(
                        ts=ts,
                        symbol=symbol,
                        timeframe=tf,
                        kind=str(d.get("type", "unknown")),
                        zone_high=float(
                            d.get("zone_high", d.get("high", float("nan")))
                        ),
                        zone_low=float(d.get("zone_low", d.get("low", float("nan")))),
                        quality=d.get("quality"),
                        score=self._safe_float(d.get("score")),
                        meta={
                            k: d.get(k)
                            for k in d.keys()
                            if k
                            not in {"type", "zone_high", "zone_low", "quality", "score"}
                        },
                    )
                )
        except Exception as e:
            self._swallow("OB", symbol, tf, e)
        return out

    def _collect_fvg(self, symbol: str, tf: str, df: pd.DataFrame) -> List[FVGRecord]:
        out: List[FVGRecord] = []
        try:
            detections = self.detectors.detect_fvg_enhanced(df) or []
            for d in detections:
                if not d:
                    continue
                ts = self._match_ts(df, d)
                out.append(
                    FVGRecord(
                        ts=ts,
                        symbol=symbol,
                        timeframe=tf,
                        direction=str(d.get("direction", "unknown")),
                        gap_top=float(d.get("gap_top", d.get("high", float("nan")))),
                        gap_bottom=float(
                            d.get("gap_bottom", d.get("low", float("nan")))
                        ),
                        quality=d.get("quality"),
                        score=self._safe_float(d.get("score")),
                        meta={
                            k: d.get(k)
                            for k in d.keys()
                            if k
                            not in {
                                "direction",
                                "gap_top",
                                "gap_bottom",
                                "quality",
                                "score",
                            }
                        },
                    )
                )
        except Exception as e:
            self._swallow("FVG", symbol, tf, e)
        return out

    def _collect_bos(self, symbol: str, tf: str, df: pd.DataFrame) -> List[BOSRecord]:
        out: List[BOSRecord] = []
        try:
            detections = self.detectors.detect_bos_mss_enhanced(df) or []
            for d in detections:
                if not d:
                    continue
                ts = self._match_ts(df, d)
                out.append(
                    BOSRecord(
                        ts=ts,
                        symbol=symbol,
                        timeframe=tf,
                        type=str(d.get("type", "unknown")),
                        level_broken=float(d.get("level_broken", float("nan"))),
                        confirmation_score=self._safe_float(
                            d.get("confirmation_score")
                        ),
                        volume_ratio=self._safe_float(d.get("volume_ratio")),
                        momentum=self._safe_float(d.get("momentum")),
                        structure_type=d.get("structure_type"),
                        quality=d.get("quality"),
                        meta={
                            k: d.get(k)
                            for k in d.keys()
                            if k
                            not in {
                                "type",
                                "level_broken",
                                "confirmation_score",
                                "volume_ratio",
                                "momentum",
                                "structure_type",
                                "quality",
                            }
                        },
                    )
                )
        except Exception as e:
            self._swallow("BOS/MSS", symbol, tf, e)
        return out

    def _collect_regime_series(
        self, symbol: str, tf: str, df: pd.DataFrame
    ) -> List[RegimeSnapshot]:
        out: List[RegimeSnapshot] = []
        try:
            # détecteur supposé retourner une Series ou colonne 'regime' déjà présente
            if "regime" in df.columns:
                regimes = df["regime"]
            else:
                regimes = self.detectors.detect_market_regime(df)
            if regimes is None or len(regimes) != len(df):
                return out
            ts_col = self._detect_ts_col(df)
            for i in range(len(df)):
                out.append(
                    RegimeSnapshot(
                        ts=(
                            str(self._to_iso(df.iloc[i][ts_col]))
                            if ts_col
                            else self._last_ts_iso(df)
                        ),
                        symbol=symbol,
                        timeframe=tf,
                        regime=str(regimes.iloc[i]),
                    )
                )
        except Exception as e:
            self._swallow("Regime", symbol, tf, e)
        return out

    def _collect_boll_micro(
        self, symbol: str, tf: str, df: pd.DataFrame
    ) -> Optional[BollRecord]:
        try:
            b = self.detectors.compute_bollinger_microphase_signals(df)
            if not b or not b.get("ok"):
                return None
            ts = self._last_ts_iso(df)
            return BollRecord(
                ts=ts,
                symbol=symbol,
                timeframe=tf,
                signal=str(b.get("signal")),
                is_range=bool(b.get("is_range")),
                is_squeeze=bool(b.get("is_squeeze")),
                is_expansion=bool(b.get("is_expansion")),
                range_score=float(b.get("range_score", 0.0) or 0.0),
                bb_mid=float(b.get("bb_mid")),
                bb_upper=float(b.get("bb_upper")),
                bb_lower=float(b.get("bb_lower")),
                dist_to_mid_pips=self._safe_float(b.get("dist_to_mid_pips")),
                dist_to_upper_pips=self._safe_float(b.get("dist_to_upper_pips")),
                dist_to_lower_pips=self._safe_float(b.get("dist_to_lower_pips")),
                mid_entry=b.get("mid_entry"),
                mid_entry_score=self._safe_float(b.get("mid_entry_score")),
                entry_gate_ok=bool(b.get("entry_gate_ok")),
                half_band_pips=self._safe_float(b.get("half_band_pips")),
                z_band=self._safe_float(b.get("z_band")),
                atr_pips=self._safe_float(b.get("atr_pips")),
                meta={
                    "squeeze_strength": b.get("squeeze_strength"),
                    "is_expansion": b.get("is_expansion"),
                    "range_duration_bars": b.get("range_duration_bars"),
                },
            )
        except Exception as e:
            self._swallow("BollMicro", symbol, tf, e)
            return None

    # --------------- Aggregations ---------------

    def _aggregate_regimes(
        self, snapshots: List[RegimeSnapshot]
    ) -> Tuple[Dict[str, int], List[Dict[str, Any]]]:
        # Distribution
        dist: Dict[str, int] = {}
        for s in snapshots:
            dist[s.regime] = dist.get(s.regime, 0) + 1

        # Transitions (regime_t-1 -> regime_t)
        transitions: Dict[Tuple[str, str], int] = {}
        prev: Optional[str] = None
        for s in snapshots:
            cur = s.regime
            if prev is not None and cur != prev:
                transitions[(prev, cur)] = transitions.get((prev, cur), 0) + 1
            prev = cur

        trans_rows = [
            {"from": a, "to": b, "count": c} for (a, b), c in transitions.items()
        ]
        trans_rows.sort(key=lambda x: (-x["count"], x["from"], x["to"]))
        return dist, trans_rows

    # --------------- Hygiene / helpers ---------------

    def _compute_hygiene(self, df: pd.DataFrame) -> Dict[str, Any]:
        bars = len(df)
        nan_ratio = float(df.isna().sum().sum()) / max(1, df.size)
        holes = (
            int(df.index.to_series().diff().gt(pd.Timedelta("90s")).sum())
            if isinstance(df.index, pd.DatetimeIndex)
            else 0
        )
        return {
            "bars": bars,
            "nan_ratio": round(nan_ratio, 6),
            "holes": holes,
            "start": self._first_ts_iso(df),
            "end": self._last_ts_iso(df),
        }

    def _detect_ts_col(self, df: pd.DataFrame) -> Optional[str]:
        for c in ("timestamp", "time", "datetime", "ts"):
            if c in df.columns:
                return c
        return None

    def _to_iso(self, ts_val) -> str:
        try:
            if hasattr(ts_val, "to_pydatetime"):
                dt = ts_val.to_pydatetime()
            elif isinstance(ts_val, (int, float)):
                # tolère ms ou s
                dt = datetime.fromtimestamp(
                    ts_val / (1000.0 if ts_val > 1e12 else 1.0), tz=timezone.utc
                )
            else:
                dt = datetime.fromisoformat(str(ts_val))
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=timezone.utc)
            return dt.astimezone(timezone.utc).isoformat()
        except Exception:
            return datetime.now(timezone.utc).isoformat()

    def _first_ts_iso(self, df: pd.DataFrame) -> str:
        ts_col = self._detect_ts_col(df)
        if ts_col:
            return self._to_iso(df.iloc[0][ts_col])
        if isinstance(df.index, pd.DatetimeIndex) and len(df.index):
            return df.index[0].astimezone(timezone.utc).isoformat()
        return datetime.now(timezone.utc).isoformat()

    def _last_ts_iso(self, df: pd.DataFrame) -> str:
        ts_col = self._detect_ts_col(df)
        if ts_col:
            return self._to_iso(df.iloc[-1][ts_col])
        if isinstance(df.index, pd.DatetimeIndex) and len(df.index):
            return df.index[-1].astimezone(timezone.utc).isoformat()
        return datetime.now(timezone.utc).isoformat()

    def _match_ts(self, df: pd.DataFrame, idx_or_dict) -> str:
        # essaie d'aligner l'événement à la dernière barre, sinon utilise la clé 'index' si fournie
        try:
            if isinstance(idx_or_dict, dict):
                if "index" in idx_or_dict and isinstance(idx_or_dict["index"], int):
                    i = max(0, min(len(df) - 1, idx_or_dict["index"]))
                    ts_col = self._detect_ts_col(df)
                    return (
                        self._to_iso(df.iloc[i][ts_col])
                        if ts_col
                        else self._last_ts_iso(df)
                    )
            return self._last_ts_iso(df)
        except Exception:
            return self._last_ts_iso(df)

    def _safe_float(self, v) -> Optional[float]:
        try:
            vf = float(v)
            return vf if math.isfinite(vf) else None
        except Exception:
            return None

    def _swallow(self, block: str, symbol: str, tf: str, e: Exception):
        self.logger.error(
            f"[Reporter] {block} failed for {symbol} {tf}: {e}", exc_info=False
        )

    # --------------- Markdown rendering ---------------

    def _render_markdown(self, report: Dict[str, Any]) -> str:
        meta = report.get("meta", {})
        stats = report.get("stats", {})
        events = report.get("events", [])

        lines: List[str] = []
        lines.append(
            f"# PhaseObserver – Rapport journalier ({meta.get('date_utc', meta.get('generated_at',''))})"
        )
        lines.append("")
        lines.append(f"- **Symbole**: `{meta.get('symbol','?')}`")
        lines.append(f"- **Timeframes**: {', '.join(meta.get('timeframes', []))}")
        lines.append(f"- **Généré**: {meta.get('generated_at','')}")
        lines.append(f"- **Version**: {meta.get('version','1.0')}")
        lines.append("")
        lines.append("## Statistiques globales")
        counts = stats.get("counts", {})
        lines.append(
            f"- OB: **{counts.get('ob',0)}** | FVG: **{counts.get('fvg',0)}** | BOS/MSS: **{counts.get('bos',0)}**"
        )
        lines.append(
            f"- Snapshots Régime: **{counts.get('regime_snapshots',0)}** | Bollinger micro: **{counts.get('boll_events',0)}**"
        )
        lines.append("")
        lines.append("### Distribution des régimes")
        for reg, c in (stats.get("regimes", {}) or {}).items():
            lines.append(f"- `{reg}`: {c}")
        lines.append("")
        if stats.get("regime_transitions"):
            lines.append("### Transitions de régime (top)")
            for t in stats["regime_transitions"][:15]:
                lines.append(f"- `{t['from']}` → `{t['to']}` : {t['count']}")
            lines.append("")

        # Sections détaillées
        def _section(title: str, key_filter: str, wanted: Iterable[str]):
            lines.append(f"## {title}")
            cnt = 0
            for evt in events:
                if (
                    isinstance(evt, dict)
                    and key_filter in evt
                    and evt.get(key_filter) in wanted
                ):
                    cnt += 1
                    lines.append(
                        f"- **{evt.get('ts','?')}** `{evt.get('symbol','?')}` `{evt.get('timeframe','?')}` → **{evt.get(key_filter)}**  "
                    )
                    # détails connus
                    for k in (
                        "zone_high",
                        "zone_low",
                        "gap_top",
                        "gap_bottom",
                        "level_broken",
                        "quality",
                        "score",
                        "confirmation_score",
                        "volume_ratio",
                        "momentum",
                        "structure_type",
                    ):
                        if k in evt and evt[k] is not None:
                            lines.append(f"  - {k}: {evt[k]}")
            if cnt == 0:
                lines.append("_Aucun évènement._")
            lines.append("")

        _section("Order Blocks", "type", ("bullish", "bearish", "unknown"))
        _section("FVG", "direction", ("up", "down", "unknown"))
        _section(
            "BOS/MSS",
            "type",
            ("bullish_bos", "bearish_bos", "bullish_mss", "bearish_mss", "unknown"),
        _section("Big Reversal Candles", "type", ("big_reversal_bullish", "big_reversal_bearish")) 
        )

        # Bollinger micro (dernière mesure par TF)
        lines.append("## Bollinger Microphase (dernière mesure par TF)")
        boll_by_tf = {}
        for evt in events:
            if evt.get("bb_mid") is not None and evt.get("bb_upper") is not None:
                boll_by_tf[evt["timeframe"]] = evt
        if not boll_by_tf:
            lines.append("_Aucune mesure disponible._")
        else:
            for tf, b in boll_by_tf.items():
                lines.append(f"### {tf}")
                lines.append(
                    f"- signal: **{b.get('signal')}** | is_range={b.get('is_range')} | squeeze={b.get('is_squeeze')} | expansion={b.get('is_expansion')}"
                )
                lines.append(
                    f"- range_score: {b.get('range_score')} | atr_pips: {b.get('atr_pips')}"
                )
                lines.append(
                    f"- mid_entry: {b.get('mid_entry')} (score={b.get('mid_entry_score')}) | entry_gate_ok={b.get('entry_gate_ok')}"
                )
                lines.append(
                    f"- dist_mid_pips: {b.get('dist_to_mid_pips')} | half_band_pips: {b.get('half_band_pips')}"
                )
                lines.append("")
        return "\n".join(lines)


if __name__ == "__main__":

    # Exemple : charger un CSV ou des données pour tester
    # ⚠️ à adapter selon ce que tu veux vraiment analyser
    dummy_df = pd.DataFrame(
        {
            "time": pd.date_range("2025-08-01", periods=100, freq="T"),
            "open": [1.1 + i * 0.0001 for i in range(100)],
            "high": [1.11 + i * 0.0001 for i in range(100)],
            "low": [1.09 + i * 0.0001 for i in range(100)],
            "close": [1.1 + i * 0.0001 for i in range(100)],
            "volume": [100 + i for i in range(100)],
        }
    )

    # Construire le reporter
    reporter = PhaseObserverReporter()

    # Lancer un rapport pour 1 symbole et plusieurs TF
    report = reporter.run_daily_report_for_asset(
        symbol="EURUSD",
        tf_frames={"M1": dummy_df, "M5": dummy_df},
    )

    # Exporter en Markdown et JSONL
    os.makedirs("reports", exist_ok=True)
    reporter.export_markdown(report, "reports/daily_report.md")
    reporter.export_jsonl(report, "reports/daily_report.jsonl")
     
    print("✅ Rapport généré dans le dossier reports/")
    
    reporter = PhaseObserverReporter()

    # Exemple : dictionnaire de DataFrames par timeframe
    tf_frames = {
        "M1": pd.DataFrame(),   # ⚠️ Ici tu mettras tes vraies données M1
        "M5": pd.DataFrame(),   # ⚠️ Ici tes données M5
    }

    # Rapport d’hier (-1)
    report = reporter.run_daily_report_for_asset("EURUSD", tf_frames, day_offset=-1)

    reporter.export_markdown(report, "reports/daily_report_yesterday.md")
