#trader/audit.py - Module d'Audit pour le Bot SNIPER_X
from __future__ import annotations

from typing import Any, Optional
from pathlib import Path
from datetime import datetime, UTC
import json
import logging
import os

import pandas as pd  # utilisé par _load_and_filter_audit_data et _export_report_data


def _log_audit_trail(self, entry: dict) -> None:
    """
    Append-only JSONL avec verrou léger (.lock).
    - Pas de double écriture.
    - Ajoute un timestamp UTC ISO s'il manque.
    - Tolérant aux erreurs (log + alerte).
    """
    from core.config_manager import CustomJSONEncoder  # import tardif
    import json

    try:
        if not isinstance(entry, dict):
            self.logger.warning("[AUDIT] Entrée non-dict ignorée.")
            return

        # Assurer répertoire
        self.audit_trail_path.parent.mkdir(parents=True, exist_ok=True)

        # Timestamp si absent
        if "timestamp" not in entry:
            entry["timestamp"] = datetime.now(UTC).isoformat()

        lock_path = self.audit_trail_path.with_suffix(".lock")

        # 1) Créer un lock exclusif (échoue si déjà présent)
        try:
            with lock_path.open("x"):
                pass
        except FileExistsError:
            # On évite la corruption ; on logge et on abandonne cette entrée
            self.logger.warning(f"[AUDIT] Lock présent ({lock_path}). Écriture annulée.")
            return

        try:
            # 2) Écriture append-only (JSONL)
            with self.audit_trail_path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(entry, cls=CustomJSONEncoder) + "\n")

            self.logger.debug(f"[AUDIT] Entrée ajoutée: {entry.get('order_id', 'N/A')}")
        finally:
            # 3) Toujours relâcher le lock
            try:
                if lock_path.exists():
                    lock_path.unlink()
            except Exception:
                # on n'échoue pas si la suppression du lock rate
                pass

    except Exception as e:
        self.logger.error(
            f"[AUDIT] Échec écriture {self.audit_trail_path}: {e}", exc_info=True
        )
        try:
            self.config_manager.send_alert(
                "CRITIQUE",
                f"Échec de l'écriture du journal d'audit: {e}",
                alert_type="telegram_critical",
            )
        except Exception:
            pass

    
def _load_and_filter_audit_data(
    self, report_date: datetime
) -> Optional[pd.DataFrame]:
    """
    Charge et filtre les données du journal d'audit pour un jour donné (UTC).
    Lecture robuste (chunkée si dispo), tolérance aux horodatages manquants/corrompus.
    """
    try:
        if not self.audit_trail_path.exists():
            self.logger.warning(
                f"Fichier d'audit introuvable à {self.audit_trail_path}. "
                f"Aucun rapport généré pour le {report_date.date()}."
            )
            return None

        report_date_utc = report_date.astimezone(UTC).date()

        filtered_parts: list[pd.DataFrame] = []

        try:
            # Lecture chunkée (mémoire-friendly)
            chunks = pd.read_json(self.audit_trail_path, lines=True, chunksize=100_000)
            for chunk in chunks:
                if "timestamp" not in chunk.columns:
                    continue
                chunk["timestamp"] = pd.to_datetime(
                    chunk["timestamp"], errors="coerce", utc=True
                )
                chunk = chunk.dropna(subset=["timestamp"])
                mask = (chunk["timestamp"].dt.date == report_date_utc)
                if mask.any():
                    filtered_parts.append(chunk.loc[mask])
            filtered_df = (
                pd.concat(filtered_parts, ignore_index=True)
                if filtered_parts
                else pd.DataFrame()
            )
        except (TypeError, ValueError):
            # Fallback: lecture complète si chunksize non supporté
            df = pd.read_json(self.audit_trail_path, lines=True)
            if "timestamp" not in df.columns:
                self.logger.warning(
                    "[AUDIT] Colonne 'timestamp' absente ; aucun filtrage possible."
                )
                return pd.DataFrame()
            df["timestamp"] = pd.to_datetime(df["timestamp"], errors="coerce", utc=True)
            df = df.dropna(subset=["timestamp"])
            filtered_df = df[df["timestamp"].dt.date == report_date_utc]

        self.logger.info(
            f"Chargé {len(filtered_df)} entrées d'audit pour le {report_date_utc}."
        )
        return filtered_df

    except FileNotFoundError:
        self.logger.warning(
            f"Fichier d'audit introuvable à {self.audit_trail_path}. Aucun rapport généré."
        )
        return None
    except Exception as e:
        self.logger.error(
            f"Erreur lors de la lecture/filtrage du journal d'audit depuis {self.audit_trail_path} : {e}",
            exc_info=True,
        )
        try:
            self.config_manager.send_alert(
                "CRITIQUE",
                f"Erreur lecture journal audit: {e}",
                alert_type="telegram_critical",
            )
        except Exception:
            pass
        return None


def _export_report_data(self, df: pd.DataFrame, report_path: Path) -> bool:
    """
    Export atomique (CSV / JSONL / Markdown).
    - CSV : format float configurable.
    - JSONL : ISO8601 pour les dates.
    - MD : titre avec date (fallback = now UTC si absent).
    """
    temp_path = report_path.with_suffix(report_path.suffix + ".tmp")
    try:
        # S'assurer que le répertoire existe
        report_path.parent.mkdir(parents=True, exist_ok=True)

        if df is None or df.empty:
            self.logger.info(f"Aucune donnée à exporter vers {report_path}.")
            return False

        report_format = report_path.suffix.lower().lstrip(".")
        if report_format == "csv":
            float_format_csv = self.config_manager.get(
                "trade_executor_settings.report_float_format_csv", "%.5f"
            )
            df.to_csv(
                temp_path,
                index=False,
                encoding="utf-8",
                float_format=float_format_csv,
            )

        elif report_format == "jsonl":
            df.to_json(
                temp_path,
                orient="records",
                lines=True,
                date_format="iso",
                default_handler=str,
            )

        elif report_format == "md":
            # Déterminer une date pour le titre
            try:
                ts0 = pd.to_datetime(df["timestamp"].iloc[0], utc=True)
                title_date = ts0.date()
            except Exception:
                title_date = datetime.now(UTC).date()

            report_title = self.config_manager.get(
                "trade_executor_settings.report_title_template",
                "# Rapport Quotidien des Trades - {date}",
            )
            with open(temp_path, "w", encoding="utf-8") as f:
                f.write(report_title.format(date=title_date) + "\n\n")
                f.write(df.to_markdown(index=False))

        else:
            raise ValueError(f"Format de rapport non supporté : {report_format}")

        # Remplacement atomique cross-platform
        temp_path.replace(report_path)
        self.logger.info(f"Rapport généré avec succès à : {report_path}")
        return True

    except Exception as e:
        self.logger.error(
            f"Échec de l'exportation du rapport vers {report_path} : {e}",
            exc_info=True,
        )
        try:
            if temp_path.exists():
                temp_path.unlink()
        except Exception:
            pass
        try:
            self.config_manager.send_alert(
                "CRITIQUE", f"Échec export rapport: {e}", alert_type="telegram_critical"
            )
        except Exception:
            pass
        return False


def generate_report(self, report_date: Optional[datetime] = None) -> Optional[Path]:
    """
    Génère un rapport quotidien d'activité à partir du journal d'audit.
    Le rapport peut être formaté en différents formats (CSV, JSONL, MD).

    Args:
        report_date (datetime, optional): La date pour laquelle générer le rapport.
                                        Par défaut, aujourd'hui en UTC.
    Returns:
        Optional[Path]: Le chemin vers le rapport généré, ou None en cas d'échec.
    """
    self.logger.info("Génération du rapport quotidien...")
    report_date = report_date if report_date else datetime.now(UTC)

    df = self._load_and_filter_audit_data(report_date)

    if df is None or df.empty:
        self.logger.info(
            f"Aucune donnée pour générer un rapport pour le {report_date.date()}. Retourne None."
        )
        return None

    # Récupérer le format de rapport depuis la configuration (dynamisé)
    report_format = self.config_manager.get(
        "trade_executor_settings.daily_report_format", "md"
    )

    # Le nom du fichier est construit avec la date et le format
    report_filename = (
        f"daily_trade_report_{report_date.strftime('%Y%m%d')}.{report_format}"
    )
    report_path = Path(
        self.config_manager.get("paths.reports", "output/")
    )  # Utilise le chemin des rapports
    full_report_filepath = report_path / report_filename

    # TODO: Ajouter des métriques de performance avancées (Profit Factor, Sharpe) au DataFrame avant export. (TODO maintenu)
    #       Ceci nécessiterait de calculer ces métriques à partir du `df` ici avant l'export.

    if self._export_report_data(
        df, full_report_filepath
    ):  # Passe le DataFrame et le chemin complet
        # Envoyer une alerte Telegram avec un résumé du rapport
        # Récupérer les métriques principales pour le message Telegram
        # Ceci est une simplification. Idéalement, _generate_report_trade_decisions devrait retourner
        # un dict de métriques que l'on pourrait utiliser ici.

        # Calcul rapide de quelques métriques pour le résumé Telegram
        total_pnl = (
            df[df["event_type"] == "TRADE_CLOSE"]["position_snapshot_at_close"]
            .apply(lambda x: x.get("profit", 0.0) if x else 0.0)
            .sum()
        )
        total_trades = (
            df[df["event_type"].isin(["TRADE_OPEN", "TRADE_CLOSE"])]
            .drop_duplicates(subset=["order_id"])
            .shape[0]
        )

        message_summary = (
            f"📊 **Rapport Quotidien des Trades - {report_date.date()}**\n\n"
        )
        message_summary += f"Trades exécutés: {total_trades}\n"
        message_summary += f"P&L Total (Estimé): `${total_pnl:.2f}`"

        # Limiter la taille du message pour Telegram
        message_summary = message_summary[:4000]  # Max length for Telegram messages

        self.config_manager.send_alert(
            message=message_summary,
            alert_type=self.config_manager.get(
                "telegram.channels.telegram_daily_report", "telegram_info"
            ),  # Utiliser le canal configuré
        )
        return full_report_filepath

    # TODO: Utiliser un moteur de templates (ex: Jinja2) pour des rapports Markdown/HTML plus riches. (TODO maintenu)
    return None

def _mark_trade_sent(self, asset: str, now_ts: float) -> None:
    """À appeler juste APRÈS un envoi d’ordre réussi."""
    if not hasattr(self, "_last_trade_ts_by_asset"):
        self._last_trade_ts_by_asset = {}
    if not hasattr(self, "_cycle_new_trades"):
        self._cycle_new_trades = 0
    self._last_trade_ts_by_asset[asset] = now_ts
    self._last_any_trade_ts = now_ts
    self._cycle_new_trades += 1