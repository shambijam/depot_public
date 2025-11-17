# phase_observer/config.py
# --- MUST BE FIRST LINE ---
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, Optional

import logging


def update_parameters_from_config(self, strategy_config: dict) -> None:
    """
    Met à jour les paramètres d'analyse à partir d'une configuration de stratégie.

    Args:
        strategy_config (dict): Le dictionnaire de configuration de la stratégie.
    """
    # Sécurité si pas de config_manager
    if getattr(self, "config_manager", None) is None:
        self.logger.warning(
            "update_parameters_from_config appelé sans config_manager. "
            "Seules les surcharges locales seront appliquées."
        )

    phase_params = (strategy_config or {}).get("phase_detection")
    strategy_name = (strategy_config or {}).get("strategy_name", "inconnue")

    if not isinstance(phase_params, dict):
        self.logger.warning(
            f"Aucun paramètre 'phase_detection' valide pour la stratégie '{strategy_name}'."
        )
        return

    # Délégué à _load_settings pour éviter la duplication de logique
    self._load_settings(overrides=phase_params)
    self.logger.info(
        f"PhaseObserver mis à jour avec les paramètres de la stratégie '{strategy_name}'."
    )
    # --- [FOOTPRINT TRIGGERS: DEFAULTS + OVERRIDES] ----------------------
    fp_user = dict(strategy_config.get("footprint_triggers", {}))
    # Defaults robustes (XAUUSD à affiner)
    fp_defaults = {
        "enabled": True,
        "filters": {
            "spread_max_pts": 35,
            "tickrate_min_per5s": 20,
            "vol_level_min_ratio_median_30s": 0.5,
        },
        "stacking": {
            "delta_ratio_min": 0.70,  # 70%
            "min_levels": 3,
            "invalidate_opposite_ratio": 0.60,
            "validity_ms": 800,  # fenêtre d’envoi post-détection
        },
        "absorption": {
            "vol_zscore_min": 2.0,
            "delta_ratio_max": 0.25,
            "attempts_min": 2,  # tentatives ratées avant rejet
        },
        "climax": {
            "lookback_bars": 20,
            "vol_ratio_min": 2.5,  # 2.5x la moyenne lookback
            "delta_ratio_min": 0.70,
            "need_consolidation": True,
            "consolidation_max_atr_mult": 0.8,  # range/ATR < 0.8 sur N barres
        },
        "order": {
            "entry_style": "LIMIT_FOK",
            "burst_count": 5,  # à ajuster, compte démo
            "burst_volume_each": 0.02,  # ex. 5x0.02 = 0.10
            "price_offset_ticks": 0,  # 0 ou -1 tick favorable
        },
    }
    fp_cfg = {**fp_defaults, **fp_user}
    # Expose dans l'instance
    setattr(self, "footprint_triggers", fp_cfg)
    # ---------------------------------------------------------------------

    # TODO: invalider/rafraîchir les caches qui dépendent des anciens paramètres (si présents)
    # ex: setattr(self, "_tf_data_cache", {})  # si tu utilises un cache interne


def _load_settings(self, overrides: Optional[Dict[str, Any]] = None):
    """
    Charge tous les paramètres depuis le ConfigManager de manière dynamique.
    Permet la surcharge de paramètres spécifiques via le dictionnaire 'overrides'.
    Nettoyage : suppression des clés inutiles liées au scalping Bollinger/Katana.
    """
    self.logger.debug("Chargement des paramètres d'analyse pour PhaseObserver...")

    # Helper deep-merge si le ConfigManager n'en propose pas
    def _deep_merge_dicts(
        base: Dict[str, Any], extra: Dict[str, Any]
    ) -> Dict[str, Any]:
        out = dict(base or {})
        for k, v in (extra or {}).items():
            if k in out and isinstance(out[k], dict) and isinstance(v, dict):
                out[k] = _deep_merge_dicts(out[k], v)
            else:
                out[k] = v
        return out

    # 1) Lire les defaults
    if getattr(self, "config_manager", None) is not None:
        try:
            all_settings = self.config_manager.get("phase_detection_defaults", {}) or {}
        except Exception as e:
            self.logger.warning(
                f"Lecture de 'phase_detection_defaults' impossible ({e}); utilisation d'un dict vide."
            )
            all_settings = {}
    else:
        all_settings = {}

    # 2) Appliquer les surcharges
    if overrides:
        self.logger.debug(f"Application de surcharges de paramètres : {overrides}")
        if getattr(self, "config_manager", None) is not None and hasattr(
            self.config_manager, "_merge_dicts"
        ):
            try:
                all_settings = self.config_manager._merge_dicts(all_settings, overrides)
            except Exception as e:
                self.logger.warning(
                    f"_merge_dicts a échoué ({e}); bascule sur deep-merge interne."
                )
                all_settings = _deep_merge_dicts(all_settings, overrides)
        else:
            all_settings = _deep_merge_dicts(all_settings, overrides)

    # 3) Filtrage : retirer les paramètres obsolètes (scalping Bollinger/Katana)
    keys_to_remove = [
        "bollinger_weights",
        "bollinger_settings",
        "scalping_confidence",
        "katana_mode",
    ]
    for k in keys_to_remove:
        if k in all_settings:
            self.logger.info(f"[CLEAN] Suppression paramètre obsolète: {k}")
            all_settings.pop(k, None)

    # 4) Affecter tous les paramètres à l'instance
    for key, value in all_settings.items():
        try:
            setattr(self, key, value)
        except Exception as e:
            self.logger.warning(f"Impossible d'appliquer le paramètre '{key}': {e}")

    # 5) Chemins de sortie (robuste même sans config_manager)
    try:
        reports_dir = (
            self.config_manager.get("paths.reports", "output/")
            if getattr(self, "config_manager", None) is not None
            else "output/"
        )
        logs_dir = (
            self.config_manager.get("paths.logs", "logs/")
            if getattr(self, "config_manager", None) is not None
            else "logs/"
        )
    except Exception:
        reports_dir, logs_dir = "output/", "logs/"

    self.output_path = Path(reports_dir)
    self.logs_dir = Path(logs_dir)
    try:
        self.output_path.mkdir(parents=True, exist_ok=True)
        self.logs_dir.mkdir(parents=True, exist_ok=True)
    except Exception as e:
        self.logger.warning(f"Création des répertoires sortie/logs impossible: {e}")

    self.logger.debug(
        "Paramètres de PhaseObserver chargés et appliqués (après nettoyage)."
    )
