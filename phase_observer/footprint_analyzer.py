# phase_observer/footprint_analyzer.py

from dataclasses import dataclass, field
from enum import Enum
from typing import Dict, Any, Tuple, Optional, List
import pandas as pd
import numpy as np
from contextlib import contextmanager
import time

# Imports de détecteurs supprimés (suppression triggers 03 DEC 2025)

"""
Footprint Trigger Analysis — Fixed & Enhanced
---------------------------------------------
- Param mapping correct vers chaque détecteur (plus d'**kwargs inattendus)
- Multi-fenêtres + double passe (normal/soft)
- Sélection du meilleur trigger (priorités cohérentes + alias)
- Fallbacks micro (stacking / absorption) si rien
- Validation & normalisation tolérantes (feeds variés)
- Observabilité (timings) sans garde-fous bloquants
"""


# Constantes de confidence supprimées (suppression triggers 03 DEC 2025)


# ========================= enums / dataclasses =========================


class ErrorCode(Enum):
    IMPORT_FAILURE = "E001"
    DATA_VALIDATION = "E002"
    DATETIME_NORMALIZATION = "E003"
    SNAPSHOT_COMPUTATION = "E004"
    TRIGGER_DETECTION = "E005"
    INSUFFICIENT_DATA = "E006"


# class TriggerType supprimée (suppression triggers 03 DEC 2025)


# class TriggerConfig supprimée (suppression triggers 03 DEC 2025)


@dataclass
class ValidationResult:
    is_valid: bool
    error_code: Optional[ErrorCode] = None
    message: str = ""
    metrics: Dict[str, Any] = field(default_factory=dict)


# class TriggerDecision supprimée (suppression triggers 03 DEC 2025)


# ========================= utilities =========================


class DataValidator:
    @staticmethod
    def validate_ticks(ticks: pd.DataFrame) -> ValidationResult:
        """
        Validation tolérante des ticks :
        - Vérifie la présence de colonnes prix/volume *ou équivalents*.
        - N'échoue plus si quelques lignes ont un prix non-positif : on valide
        si au moins un sous-ensemble exploitable (prix > 0) existe.
        - Expose des métriques utiles (lignes totales, lignes valides).
        """
        if ticks is None or ticks.empty:
            return ValidationResult(
                False, ErrorCode.INSUFFICIENT_DATA, "Ticks dataframe is empty"
            )

        cols = set(ticks.columns)

        has_price_like = (
            ("price" in cols) or ({"bid", "ask"} <= cols) or ("last" in cols)
        )
        has_vol_like = any(
            c in cols for c in ("volume", "size", "qty", "amount", "vol")
        )
        if not has_price_like or not has_vol_like:
            return ValidationResult(
                False, ErrorCode.DATA_VALIDATION, "Missing price/volume-like columns"
            )

        # Série de prix "effective" (sans muter le DF d'entrée)
        eff_price = None
        try:
            if "price" in cols:
                eff_price = pd.to_numeric(ticks["price"], errors="coerce")
            elif {"bid", "ask"} <= cols:
                bid = pd.to_numeric(ticks["bid"], errors="coerce")
                ask = pd.to_numeric(ticks["ask"], errors="coerce")
                eff_price = (bid + ask) * 0.5
            elif "last" in cols:
                eff_price = pd.to_numeric(ticks["last"], errors="coerce")
        except Exception:
            eff_price = None

        if eff_price is None:
            return ValidationResult(
                False, ErrorCode.DATA_VALIDATION, "No usable price series"
            )

        good_price_mask = eff_price.notna() & (eff_price > 0)
        valid_rows = int(good_price_mask.sum())
        total_rows = int(len(ticks))

        if valid_rows == 0:
            return ValidationResult(
                False, ErrorCode.DATA_VALIDATION, "Detected non-positive prices"
            )

        # (Optionnel) check volume > 0 sur une colonne connue, sans bloquer si mixte
        pos_vol_rows = None
        try:
            vol_col = next(
                (c for c in ("volume", "size", "qty", "amount", "vol") if c in cols),
                None,
            )
            if vol_col is not None:
                vol_series = pd.to_numeric(ticks[vol_col], errors="coerce")
                pos_vol_rows = int((vol_series.fillna(0) > 0).sum())
        except Exception:
            pos_vol_rows = None

        return ValidationResult(
            True,
            metrics={
                "row_count": total_rows,
                "valid_price_rows": valid_rows,
                "positive_volume_rows": pos_vol_rows,
            },
        )

    @staticmethod
    def validate_bars(bars: pd.DataFrame) -> ValidationResult:
        if bars is None or bars.empty:
            return ValidationResult(True, message="Bars optional, skipping")
        cols = set(bars.columns)
        required = {"open", "high", "low", "close"}
        missing = required - cols
        if missing:
            return ValidationResult(
                False, ErrorCode.DATA_VALIDATION, f"Bars missing: {sorted(missing)}"
            )
        try:
            invalid = (
                (bars["high"] < bars["low"])
                | (bars["high"] < bars["open"])
                | (bars["high"] < bars["close"])
                | (bars["low"] > bars["open"])
                | (bars["low"] > bars["close"])
            )
            if invalid.any():
                return ValidationResult(
                    False,
                    ErrorCode.DATA_VALIDATION,
                    f"Invalid OHLC in {int(invalid.sum())} bars",
                )
        except Exception as e:
            return ValidationResult(
                False, ErrorCode.DATA_VALIDATION, f"OHLC validation failed: {e}"
            )

        vol_col = None
        for c in ("volume", "tick_volume", "tickVolume", "vol"):
            if c in cols:
                vol_col = c
                break
        return ValidationResult(
            True, metrics={"bar_count": int(len(bars)), "vol_col": vol_col}
        )


class DatetimeNormalizer:
    @staticmethod
    def normalize_series(s: pd.Series) -> pd.Series:
        s = pd.to_datetime(s, errors="coerce")
        if pd.api.types.is_datetime64tz_dtype(s):
            s = s.dt.tz_convert("UTC").dt.tz_localize(None)
        return s

    @staticmethod
    def normalize_index(idx: pd.Index) -> pd.Index:
        idx = pd.to_datetime(idx, errors="coerce")
        if getattr(idx, "tz", None):
            idx = idx.tz_convert("UTC").tz_localize(None)
        return idx

    @classmethod
    def normalize_dataframe(
        cls, df: pd.DataFrame, prefer_col: str = "dt"
    ) -> pd.DataFrame:
        if df is None or df.empty:
            return df
        if pd.api.types.is_datetime64_any_dtype(df.index):
            df.index = cls.normalize_index(df.index)
        dt_cols = [
            c for c in ("dt", "datetime", "timestamp", "time") if c in df.columns
        ]
        for c in dt_cols:
            if pd.api.types.is_datetime64_any_dtype(
                df[c]
            ) or pd.api.types.is_object_dtype(df[c]):
                df[c] = cls.normalize_series(df[c])
        picked = next(
            (
                c
                for c in (prefer_col, "dt", "datetime", "timestamp", "time")
                if c in df.columns and pd.api.types.is_datetime64_any_dtype(df[c])
            ),
            None,
        )
        if picked is None and pd.api.types.is_datetime64_any_dtype(df.index):
            df["dt"] = df.index
            picked = "dt"
        if picked:
            df = df[~df[picked].isna()].sort_values(picked)
        return df


# class ConfidenceScorer supprimée (suppression triggers 03 DEC 2025)


class PerformanceMonitor:
    @contextmanager
    def measure_phase(self, phase_name: str):
        start = time.perf_counter()
        try:
            yield
        finally:
            elapsed_ms = (time.perf_counter() - start) * 1000.0
            self._record_metric(f"footprint.{phase_name}.duration_ms", elapsed_ms)

    def _record_metric(self, metric_name: str, value: float):
        # Plug vers Prometheus/StatsD si besoin
        pass


# ========================= main analyzer =========================


class FootprintAnalyzer:
    def __init__(
        self, logger=None, metrics_monitor: Optional[PerformanceMonitor] = None
    ):
        self.logger = logger
        self.monitor = metrics_monitor or PerformanceMonitor()
        self.validator = DataValidator()
        self.normalizer = DatetimeNormalizer()
        # self.scorer et self._last_signal supprimés (suppression triggers 03 DEC 2025)

    # --- SOFT-QUIET-XAU: helpers ---
    def _fp_settings(self, strategy_config):
        node = (strategy_config or {}).get("logging", {})
        # modes: off | soft | verbose (par défaut: soft)
        mode = str(node.get("footprint_mode", "soft")).lower()
        enabled = set(
            map(
                str.upper,
                (strategy_config or {})
                .get("footprint", {})
                .get("enabled_assets", ["XAUUSD"]),
            )
        )
        return mode, enabled

    def _fp_can_log(self, asset_upper: str, tag: str) -> bool:
        # tag ∈ {"SUMMARY","SNAPSHOT","DETAIL"}
        if self._fp_mode == "off":
            return False
        if self._fp_mode == "soft" and tag != "SUMMARY":
            return False
        return asset_upper in self._fp_enabled

    def _fp_log(self, tag: str, fmt: str, *args, level: str = "info"):
        if not getattr(self, "logger", None):
            return
        if not self._fp_can_log(self._asset_upper, tag):
            return
        getattr(self.logger, level, self.logger.info)(fmt, *args)

    def _log_error(
        self, where: str, exc: Exception, extra: Optional[Dict[str, Any]] = None
    ):
        if not getattr(self, "logger", None):
            return
        try:
            self.logger.debug("[FP-ERR][%s] %s extra=%s", where, repr(exc), extra or {})
        except Exception:
            pass


    # Méthode analyze_footprint_triggers supprimée (suppression triggers 03 DEC 2025)

    # ---- internals ----------------------------------------------------



    # ------------------- helpers -------------------

    def _ensure_price_volume_columns(self, ticks: pd.DataFrame) -> pd.DataFrame:
        """
        Normalisation robuste du flux ticks :
        - Crée/corrige la colonne 'price' même si une 'price' exotique existe mais contient des valeurs <= 0.
        - Sources de vérité, par ordre de priorité : mid(bid,ask) → last → price existante → bid → ask.
        - Remplissages ciblés (ffill/bfill) puis purge des lignes non-positives.
        - Aligne 'volume' depuis une colonne volume-like si besoin.
        """
        if ticks is None or ticks.empty:
            return ticks

        df = ticks.copy()

        # --- Prépare les champs disponibles ---
        cols = set(df.columns)
        has_bid = "bid" in cols
        has_ask = "ask" in cols
        has_bid_ask = has_bid and has_ask
        has_last = "last" in cols
        has_price = "price" in cols

        # Cast numériques sans lever d'exception
        def _to_num(s):
            try:
                return pd.to_numeric(s, errors="coerce")
            except Exception:
                return s

        mid = None
        if has_bid:
            df["bid"] = _to_num(df["bid"])
        if has_ask:
            df["ask"] = _to_num(df["ask"])
        if has_bid_ask:
            mid = (df["bid"] + df["ask"]) * 0.5

        if has_last:
            df["last"] = _to_num(df["last"])

        if has_price:
            df["price"] = _to_num(df["price"])

        # --- Construction / réparation de 'price' ---
        if "price" not in df.columns:
            # Crée 'price' à partir des meilleures sources
            if has_bid_ask:
                df["price"] = mid
            elif has_last:
                df["price"] = df["last"]
            elif has_bid:
                df["price"] = df["bid"]
            elif has_ask:
                df["price"] = df["ask"]
            else:
                # Pas de source fiable → on crée vide (le validateur gèrera)
                df["price"] = np.nan
        else:
            # Répare la 'price' existante si <= 0 ou NaN
            bad_mask = ~np.isfinite(df["price"]) | (df["price"] <= 0)
            if bad_mask.any():
                # 1) remplace d'abord par mid si dispo
                if has_bid_ask:
                    df.loc[bad_mask, "price"] = mid[bad_mask]
                    bad_mask = ~np.isfinite(df["price"]) | (df["price"] <= 0)
                # 2) sinon par 'last' si dispo
                if bad_mask.any() and has_last:
                    df.loc[bad_mask, "price"] = df["last"][bad_mask]
                    bad_mask = ~np.isfinite(df["price"]) | (df["price"] <= 0)
                # 3) sinon par bid/ask si dispo
                if bad_mask.any() and has_bid:
                    df.loc[bad_mask, "price"] = df["bid"][bad_mask]
                    bad_mask = ~np.isfinite(df["price"]) | (df["price"] <= 0)
                if bad_mask.any() and has_ask:
                    df.loc[bad_mask, "price"] = df["ask"][bad_mask]
                    bad_mask = ~np.isfinite(df["price"]) | (df["price"] <= 0)
                # 4) ffill/bfill comme dernier recours (utile si flux sporadique)
                if bad_mask.any():
                    df["price"] = df["price"].ffill().bfill()

        # --- Filtre final: supprimer les lignes au prix non-positif ---
        df = df[pd.to_numeric(df["price"], errors="coerce") > 0].copy()

        # --- Volume: crée 'volume' si absent depuis les colonnes connues ---
        if "volume" not in df.columns:
            for alt in ("volume", "size", "qty", "amount", "vol"):
                if alt in df.columns:
                    df["volume"] = _to_num(df[alt])
                    break
            if "volume" not in df.columns:
                # On laisse sans volume (le validateur pourra refuser si vraiment nécessaire)
                df["volume"] = np.nan

        return df


# ========================= wrapper (même signature) =========================
# -> drop-in : si un vieux appel importe directement cette fonction


