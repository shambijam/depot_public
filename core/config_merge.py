# core/config_merge.py
"""
ConfigMerger - Chef d'atelier pour la fusion des configurations.

Analogie:
- ConfigLoader = Ouvrier (lit UN fichier, vérifie le format)
- ConfigMerger = Chef d'atelier (sait quels fichiers charger, les assemble, donne le produit final)

Ce module résout le problème de l'ajout de nouveaux actifs:
- Pour NAS100: charge strategy_base + NAS100.json overrides
- Pour EURUSD: charge strategy_base + EURUSD.json overrides
- Pour tout nouvel actif: même logique, zéro modification de code

Usage:
    merger = ConfigMerger(config_loader)
    merged_config = merger.get_merged_config("NAS100", "scalping")
"""

import logging
import copy
from pathlib import Path
from typing import Dict, Any, Optional, List

logger = logging.getLogger(__name__)


class ConfigMerger:
    """
    Chef d'atelier qui orchestre le chargement et la fusion des configurations.

    Responsabilités:
    1. Savoir quels fichiers charger pour chaque actif/stratégie
    2. Fusionner dans le bon ordre (base → overrides)
    3. Appliquer les overrides aux bons chemins
    4. Fournir une config finale prête à l'emploi
    """

    # Mapping stratégie → fichier de config base
    STRATEGY_CONFIG_MAP = {
        "scalping": "config/strategy/config_trade_scalping.json",
        # Ajoutez d'autres stratégies ici si nécessaire
        # "liquidity": "config/strategy/config_trade_liquidity.json",
    }

    # Dossier des configs d'actifs
    ASSETS_CONFIG_DIR = "config/assets_config"

    def __init__(self, config_loader_instance):
        """
        Initialise le ConfigMerger.

        Args:
            config_loader_instance: Instance de ConfigLoader (l'ouvrier qui lit les fichiers)
        """
        self.config_loader = config_loader_instance
        self.logger = logging.getLogger(__name__)

        # Cache des configs fusionnées (clé: "SYMBOL_strategy")
        self._merged_cache: Dict[str, Dict[str, Any]] = {}

        self.logger.info("ConfigMerger initialisé (Chef d'atelier)")

    def get_merged_config(
        self,
        asset_symbol: str,
        strategy_name: str = "scalping",
        force_reload: bool = False
    ) -> Dict[str, Any]:
        """
        Retourne la configuration fusionnée pour un actif et une stratégie.

        Ordre de fusion (priorité croissante):
        1. Config de base de la stratégie (config_trade_scalping.json)
        2. Overrides spécifiques à l'actif (NAS100.json → overrides.scalping.*)

        Args:
            asset_symbol: Symbole de l'actif (ex: "NAS100", "EURUSD")
            strategy_name: Nom de la stratégie (ex: "scalping")
            force_reload: Si True, ignore le cache et recharge depuis les fichiers

        Returns:
            Dict[str, Any]: Configuration fusionnée prête à l'emploi
        """
        asset_symbol = str(asset_symbol or "").upper().strip()
        strategy_name = str(strategy_name or "scalping").lower().strip()

        cache_key = f"{asset_symbol}_{strategy_name}"

        # Vérifier le cache
        if not force_reload and cache_key in self._merged_cache:
            self.logger.debug(f"[CACHE HIT] Config fusionnée pour {cache_key}")
            return copy.deepcopy(self._merged_cache[cache_key])

        self.logger.info(f"[MERGE] Fusion config pour {asset_symbol} / {strategy_name}...")

        # 1. Charger la config de base de la stratégie
        strategy_config = self._load_strategy_base(strategy_name)
        if not strategy_config:
            self.logger.error(f"Impossible de charger la config de base pour '{strategy_name}'")
            return {}

        # 2. Charger la config de l'actif
        asset_config = self._load_asset_config(asset_symbol)
        if not asset_config:
            self.logger.warning(f"Pas de config spécifique pour {asset_symbol}, utilisation de la config de base")
            self._merged_cache[cache_key] = strategy_config
            return copy.deepcopy(strategy_config)

        # 3. Fusionner les configs
        merged = self._merge_configs(strategy_config, asset_config, strategy_name)

        # 4. Ajouter les métadonnées de l'actif
        merged = self._inject_asset_metadata(merged, asset_config)

        # 5. Mettre en cache
        self._merged_cache[cache_key] = merged

        self.logger.info(f"[MERGE OK] Config fusionnée pour {asset_symbol}/{strategy_name}")
        return copy.deepcopy(merged)

    def _load_strategy_base(self, strategy_name: str) -> Dict[str, Any]:
        """Charge la config de base d'une stratégie."""
        if strategy_name not in self.STRATEGY_CONFIG_MAP:
            self.logger.error(f"Stratégie inconnue: {strategy_name}")
            return {}

        config_path = Path(self.STRATEGY_CONFIG_MAP[strategy_name])

        # Chemin absolu si nécessaire
        if not config_path.is_absolute():
            config_path = Path(__file__).parent.parent / config_path

        if not config_path.is_file():
            self.logger.error(f"Fichier de stratégie introuvable: {config_path}")
            return {}

        try:
            config = self.config_loader.parse_json_config(str(config_path))
            self.logger.debug(f"Config stratégie '{strategy_name}' chargée depuis {config_path}")
            return config
        except Exception as e:
            self.logger.error(f"Erreur chargement config stratégie '{strategy_name}': {e}")
            return {}

    def _load_asset_config(self, asset_symbol: str) -> Dict[str, Any]:
        """Charge la config spécifique d'un actif."""
        config_path = Path(__file__).parent.parent / self.ASSETS_CONFIG_DIR / f"{asset_symbol}.json"

        if not config_path.is_file():
            self.logger.debug(f"Pas de config spécifique pour {asset_symbol}")
            return {}

        try:
            config = self.config_loader.parse_json_config(str(config_path))
            self.logger.debug(f"Config actif '{asset_symbol}' chargée depuis {config_path}")
            return config
        except Exception as e:
            self.logger.error(f"Erreur chargement config actif '{asset_symbol}': {e}")
            return {}

    def _merge_configs(
        self,
        strategy_config: Dict[str, Any],
        asset_config: Dict[str, Any],
        strategy_name: str
    ) -> Dict[str, Any]:
        """
        Fusionne la config de stratégie avec les overrides de l'actif.

        Les overrides de l'actif (asset_config['overrides']['scalping']) sont appliqués
        sur les chemins correspondants dans la config de stratégie.
        """
        merged = copy.deepcopy(strategy_config)

        # Récupérer les overrides spécifiques à la stratégie
        overrides = asset_config.get("overrides", {}).get(strategy_name, {})

        if not overrides:
            self.logger.debug(f"Pas d'overrides pour la stratégie '{strategy_name}'")
            return merged

        self.logger.debug(f"Application des overrides: {list(overrides.keys())}")

        # Appliquer les overrides sur entry_rules.scalping.burst_scalping
        entry_rules = merged.get("entry_rules", {})
        strategy_entry = entry_rules.get(strategy_name, {})
        burst_scalping = strategy_entry.get("burst_scalping", {})

        # Fusion des sections d'overrides
        for section_key, section_value in overrides.items():
            if section_key in burst_scalping:
                # Deep merge pour les sections existantes
                burst_scalping[section_key] = self._deep_merge(
                    burst_scalping[section_key],
                    section_value
                )
                self.logger.debug(f"[OVERRIDE] {section_key} fusionné dans burst_scalping")
            else:
                # Ajout direct pour les nouvelles sections
                burst_scalping[section_key] = copy.deepcopy(section_value)
                self.logger.debug(f"[OVERRIDE] {section_key} ajouté à burst_scalping")

        # Gestion spéciale: entry_rules dans overrides → doit écraser entry_rules.scalping
        if "entry_rules" in overrides:
            asset_entry_rules = overrides["entry_rules"]
            for strat_key, strat_value in asset_entry_rules.items():
                if strat_key in entry_rules:
                    entry_rules[strat_key] = self._deep_merge(
                        entry_rules[strat_key],
                        strat_value
                    )
                else:
                    entry_rules[strat_key] = copy.deepcopy(strat_value)
            self.logger.debug("[OVERRIDE] entry_rules fusionné")

        # Remettre dans la structure
        strategy_entry["burst_scalping"] = burst_scalping
        entry_rules[strategy_name] = strategy_entry
        merged["entry_rules"] = entry_rules

        return merged

    def _inject_asset_metadata(
        self,
        merged: Dict[str, Any],
        asset_config: Dict[str, Any]
    ) -> Dict[str, Any]:
        """
        Injecte les métadonnées de l'actif dans la config fusionnée.

        Métadonnées: symbol, type, symbol_info, volatility, risk_management, etc.
        """
        # Liste des clés de métadonnées à injecter
        metadata_keys = [
            "symbol",
            "type",
            "profile",
            "description",
            "symbol_info",
            "volatility",
            "risk_management",
            "strategy_toggles",
            "strategy_whitelist",
        ]

        for key in metadata_keys:
            if key in asset_config:
                merged[f"_asset_{key}"] = copy.deepcopy(asset_config[key])

        # Injecter symbol_info directement au niveau racine aussi (compatibilité)
        if "symbol_info" in asset_config:
            merged["symbol_info"] = copy.deepcopy(asset_config["symbol_info"])

        # Injecter entry_rules de l'actif (pour sltp notamment)
        if "entry_rules" in asset_config:
            asset_entry_rules = asset_config["entry_rules"]
            merged_entry_rules = merged.get("entry_rules", {})
            merged["entry_rules"] = self._deep_merge(merged_entry_rules, asset_entry_rules)
            self.logger.debug("[METADATA] entry_rules de l'actif injecté")

        return merged

    def _deep_merge(self, base: Dict[str, Any], override: Dict[str, Any]) -> Dict[str, Any]:
        """
        Fusionne récursivement deux dictionnaires.
        Les valeurs de `override` écrasent celles de `base`.
        """
        result = copy.deepcopy(base)

        for key, value in override.items():
            if (
                key in result
                and isinstance(result[key], dict)
                and isinstance(value, dict)
            ):
                result[key] = self._deep_merge(result[key], value)
            else:
                result[key] = copy.deepcopy(value)

        return result

    def clear_cache(self, asset_symbol: Optional[str] = None) -> None:
        """
        Vide le cache des configs fusionnées.

        Args:
            asset_symbol: Si fourni, vide uniquement le cache pour cet actif.
                         Si None, vide tout le cache.
        """
        if asset_symbol:
            asset_upper = asset_symbol.upper()
            keys_to_remove = [k for k in self._merged_cache if k.startswith(asset_upper)]
            for key in keys_to_remove:
                del self._merged_cache[key]
            self.logger.info(f"Cache vidé pour {asset_symbol} ({len(keys_to_remove)} entrées)")
        else:
            count = len(self._merged_cache)
            self._merged_cache.clear()
            self.logger.info(f"Cache entièrement vidé ({count} entrées)")

    def get_sltp_config(
        self,
        asset_symbol: str,
        strategy_name: str = "scalping"
    ) -> Dict[str, Any]:
        """
        Raccourci pour obtenir la config SLTP fusionnée pour un actif.

        Cette méthode simplifie l'accès aux paramètres SL/TP en cherchant
        dans l'ordre de priorité correct.

        Returns:
            Dict avec les clés: sl_pips, tp_pips, sl_method, tp_method, rr_base, etc.
        """
        merged = self.get_merged_config(asset_symbol, strategy_name)

        # Chemins possibles pour sltp (ordre de priorité)
        sltp_paths = [
            # 1. entry_rules.scalping.burst_scalping.sltp (priorité haute - asset override)
            ["entry_rules", strategy_name, "burst_scalping", "sltp"],
            # 2. overrides.scalping.sltp (dans asset config)
            ["overrides", strategy_name, "sltp"],
            # 3. entry_rules.scalping.sltp (fallback)
            ["entry_rules", strategy_name, "sltp"],
        ]

        for path in sltp_paths:
            sltp = self._get_nested(merged, path)
            if sltp and isinstance(sltp, dict) and sltp.get("sl"):
                self.logger.debug(f"[SLTP] Trouvé dans {'.'.join(path)}")
                return self._flatten_sltp(sltp)

        self.logger.warning(f"[SLTP] Aucune config SLTP trouvée pour {asset_symbol}")
        return {}

    def _get_nested(self, data: Dict[str, Any], path: List[str]) -> Any:
        """Récupère une valeur imbriquée dans un dict."""
        current = data
        for key in path:
            if isinstance(current, dict) and key in current:
                current = current[key]
            else:
                return None
        return current

    def _flatten_sltp(self, sltp: Dict[str, Any]) -> Dict[str, Any]:
        """
        Aplatit la config SLTP pour un accès facile.

        Transforme:
            {"sl": {"pips": 800}, "tp": {"pips": 1200}, "rr_base": 1.5}
        En:
            {"sl_pips": 800, "tp_pips": 1200, "rr_base": 1.5, ...}
        """
        result = {}

        # SL
        sl = sltp.get("sl", {})
        if isinstance(sl, dict):
            result["sl_pips"] = sl.get("pips")
            result["sl_buffer_pips"] = sl.get("buffer_pips")
            result["sl_atr_multiplier"] = sl.get("atr_multiplier")

        # TP
        tp = sltp.get("tp", {})
        if isinstance(tp, dict):
            result["tp_pips"] = tp.get("pips")
            result["tp_atr_multiplier"] = tp.get("atr_multiplier")
            result["min_sl_tp_distance_pips"] = (tp.get("execution", {}) or {}).get("min_sl_tp_distance_pips")

        # Autres paramètres
        result["sl_method"] = sltp.get("sl_method")
        result["tp_method"] = sltp.get("tp_method")
        result["exit_mode"] = sltp.get("exit_mode")
        result["rr_base"] = sltp.get("rr_base")
        result["rr_floor"] = sltp.get("rr_floor")
        result["rr_cap"] = sltp.get("rr_cap")

        # Nettoyer les None
        result = {k: v for k, v in result.items() if v is not None}

        return result

    def list_available_assets(self) -> List[str]:
        """Liste tous les actifs ayant une config disponible."""
        assets_dir = Path(__file__).parent.parent / self.ASSETS_CONFIG_DIR
        if not assets_dir.is_dir():
            return []

        return [f.stem for f in assets_dir.glob("*.json")]

    def validate_asset_config(self, asset_symbol: str) -> Dict[str, Any]:
        """
        Valide la config d'un actif et retourne un rapport.

        Utile pour diagnostiquer les problèmes de configuration.
        """
        report = {
            "asset": asset_symbol,
            "valid": True,
            "warnings": [],
            "errors": [],
        }

        asset_config = self._load_asset_config(asset_symbol)

        if not asset_config:
            report["valid"] = False
            report["errors"].append(f"Config introuvable pour {asset_symbol}")
            return report

        # Vérifications
        required_keys = ["symbol", "symbol_info"]
        for key in required_keys:
            if key not in asset_config:
                report["warnings"].append(f"Clé '{key}' manquante")

        # Vérifier symbol_info
        symbol_info = asset_config.get("symbol_info", {})
        required_symbol_info = ["point", "digits"]
        for key in required_symbol_info:
            if key not in symbol_info:
                report["errors"].append(f"symbol_info.{key} manquant (CRITIQUE)")
                report["valid"] = False

        # Vérifier les overrides scalping
        scalping_overrides = asset_config.get("overrides", {}).get("scalping", {})
        if not scalping_overrides:
            report["warnings"].append("Pas d'overrides scalping définis")

        # Vérifier sltp
        sltp = self.get_sltp_config(asset_symbol)
        if not sltp.get("sl_pips"):
            report["warnings"].append("sl_pips non défini dans SLTP")
        if not sltp.get("tp_pips"):
            report["warnings"].append("tp_pips non défini dans SLTP")

        return report
