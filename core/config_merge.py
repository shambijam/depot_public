# core/config_merge.py
"""
ConfigMerger - Chef d'atelier pour la fusion des configurations.

Analogie:
- ConfigLoader = Ouvrier (lit UN fichier, verifie le format)
- ConfigMerger = Chef d'atelier (sait quels fichiers charger, les assemble, donne le produit final)

Ce module resout le probleme de l'ajout de nouveaux actifs:
- Pour NAS100: charge strategy_base + NAS100.json overrides
- Pour EURUSD: charge strategy_base + EURUSD.json overrides
- Pour tout nouvel actif: meme logique, zero modification de code

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

    Responsabilites:
    1. Savoir quels fichiers charger pour chaque actif/strategie
    2. Fusionner dans le bon ordre (base -> overrides)
    3. Appliquer les overrides aux bons chemins (burst_scalping)
    4. Fournir une config finale prete a l'emploi
    """

    # Mapping strategie -> fichier de config base
    STRATEGY_CONFIG_MAP = {
        "scalping": "config/strategy/config_trade_scalping.json",
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

        # Cache des configs fusionnees (cle: "SYMBOL_strategy")
        self._merged_cache: Dict[str, Dict[str, Any]] = {}

        self.logger.info("ConfigMerger initialise (Chef d'atelier)")

    def debug_merge_process(self, asset_symbol: str, strategy_name: str = "scalping"):
        """
        Debug complet du processus de fusion.
        """
        self.logger.info("=" * 80)
        self.logger.info(f"[DEBUG_MERGE] {asset_symbol} / {strategy_name}")
        self.logger.info("=" * 80)

        # 1. Charger les configs separement
        strategy_config = self._load_strategy_base(strategy_name)
        asset_config = self._load_asset_config(asset_symbol)

        self.logger.info("CONFIG DE BASE DE LA STRATEGIE:")
        if strategy_config:
            # Chercher dans entry_rules.scalping.burst_scalping.closure_rules
            try:
                base_closure = (
                    strategy_config.get("entry_rules", {})
                    .get(strategy_name, {})
                    .get("burst_scalping", {})
                    .get("closure_rules", {})
                )
                if base_closure:
                    self.logger.info(f"  closure_rules dans burst_scalping: {base_closure}")
                else:
                    self.logger.info("  closure_rules: ABSENT de burst_scalping")
            except Exception:
                pass
        else:
            self.logger.error("  Impossible de charger la config de base")

        self.logger.info("")
        self.logger.info("CONFIG DE L'ACTIF:")
        if asset_config:
            self.logger.info(f"  Config chargee pour {asset_symbol}")

            # Chercher overrides.scalping.closure_rules
            overrides = asset_config.get("overrides", {}).get(strategy_name, {})
            if "closure_rules" in overrides:
                self.logger.info(f"  closure_rules dans overrides: {overrides['closure_rules']}")
            else:
                self.logger.info(f"  closure_rules PAS dans overrides.{strategy_name}")
                self.logger.info(f"  Overrides disponibles: {list(overrides.keys())}")
        else:
            self.logger.warning(f"  Pas de config specifique pour {asset_symbol}")

        self.logger.info("")
        self.logger.info("PROCESSUS DE FUSION:")

        # 2. Simuler la fusion
        merged = self._merge_configs(strategy_config or {}, asset_config or {}, strategy_name)

        self.logger.info("")
        self.logger.info("CONFIG FUSIONNEE FINALE:")

        # Verifier dans burst_scalping (pas a la racine)
        burst_closure = (
            merged.get("entry_rules", {})
            .get(strategy_name, {})
            .get("burst_scalping", {})
            .get("closure_rules", {})
        )
        if burst_closure:
            self.logger.info(f"  closure_rules dans burst_scalping:")
            for k, v in burst_closure.items():
                self.logger.info(f"     {k}: {v}")
        else:
            self.logger.error("  closure_rules ABSENT de burst_scalping!")

        # Verifier qu'il n'est PAS a la racine
        if "closure_rules" in merged:
            self.logger.warning("  ATTENTION: closure_rules aussi present a la racine (ne devrait pas)")

        self.logger.info("=" * 80)

    def get_merged_config(
        self,
        asset_symbol: str,
        strategy_name: str = "scalping",
        force_reload: bool = False,
    ) -> Dict[str, Any]:
        """
        Retourne la configuration fusionnee pour un actif et une strategie.

        Ordre de fusion (priorite croissante):
        1. Config de base de la strategie (config_trade_scalping.json)
        2. Overrides specifiques a l'actif (NAS100.json -> overrides.scalping.*)

        Args:
            asset_symbol: Symbole de l'actif (ex: "NAS100", "EURUSD")
            strategy_name: Nom de la strategie (ex: "scalping")
            force_reload: Si True, ignore le cache et recharge depuis les fichiers

        Returns:
            Dict[str, Any]: Configuration fusionnee prete a l'emploi
        """
        asset_symbol = str(asset_symbol or "").upper().strip()
        strategy_name = str(strategy_name or "scalping").lower().strip()

        cache_key = f"{asset_symbol}_{strategy_name}"

        # Verifier le cache
        if not force_reload and cache_key in self._merged_cache:
            self.logger.debug(f"[CACHE HIT] Config fusionnee pour {cache_key}")
            return copy.deepcopy(self._merged_cache[cache_key])

        self.logger.info(
            f"[MERGE] Fusion config pour {asset_symbol} / {strategy_name}..."
        )

        # 1. Charger la config de base de la strategie
        strategy_config = self._load_strategy_base(strategy_name)
        if not strategy_config:
            self.logger.error(
                f"Impossible de charger la config de base pour '{strategy_name}'"
            )
            return {}

        # 2. Charger la config de l'actif
        asset_config = self._load_asset_config(asset_symbol)
        if not asset_config:
            self.logger.warning(
                f"Pas de config specifique pour {asset_symbol}, utilisation de la config de base"
            )
            self._merged_cache[cache_key] = strategy_config
            return copy.deepcopy(strategy_config)

        # 3. Fusionner les configs
        merged = self._merge_configs(strategy_config, asset_config, strategy_name)

        # 4. Ajouter les metadonnees de l'actif
        merged = self._inject_asset_metadata(merged, asset_config)

        # 5. Mettre en cache
        self._merged_cache[cache_key] = merged

        self.logger.info(
            f"[MERGE OK] Config fusionnee pour {asset_symbol}/{strategy_name}"
        )
        return copy.deepcopy(merged)

    def _load_strategy_base(self, strategy_name: str) -> Dict[str, Any]:
        """Charge la config de base d'une strategie."""
        if strategy_name not in self.STRATEGY_CONFIG_MAP:
            self.logger.error(f"Strategie inconnue: {strategy_name}")
            return {}

        config_path = Path(self.STRATEGY_CONFIG_MAP[strategy_name])

        # Chemin absolu si necessaire
        if not config_path.is_absolute():
            config_path = Path(__file__).parent.parent / config_path

        if not config_path.is_file():
            self.logger.error(f"Fichier de strategie introuvable: {config_path}")
            return {}

        try:
            config = self.config_loader.parse_json_config(str(config_path))
            self.logger.debug(
                f"Config strategie '{strategy_name}' chargee depuis {config_path}"
            )
            return config
        except Exception as e:
            self.logger.error(
                f"Erreur chargement config strategie '{strategy_name}': {e}"
            )
            return {}

    def _load_asset_config(self, asset_symbol: str) -> Dict[str, Any]:
        """Charge la config specifique d'un actif."""
        config_path = (
            Path(__file__).parent.parent
            / self.ASSETS_CONFIG_DIR
            / f"{asset_symbol}.json"
        )

        if not config_path.is_file():
            self.logger.debug(f"Pas de config specifique pour {asset_symbol}")
            return {}

        try:
            config = self.config_loader.parse_json_config(str(config_path))
            self.logger.debug(
                f"Config actif '{asset_symbol}' chargee depuis {config_path}"
            )
            return config
        except Exception as e:
            self.logger.error(f"Erreur chargement config actif '{asset_symbol}': {e}")
            return {}

    def _merge_configs(
        self,
        strategy_config: Dict[str, Any],
        asset_config: Dict[str, Any],
        strategy_name: str,
    ) -> Dict[str, Any]:
        """
        Fusionne la config de strategie avec les overrides de l'actif.

        TOUTES les sections d'overrides (closure_rules, sltp, footprint,
        orderflow_v6, timing_gatekeeper, sniper_mode, etc.) vont dans
        entry_rules.{strategy_name}.burst_scalping.

        closure_rules va dans burst_scalping, PAS a la racine.
        """
        merged = copy.deepcopy(strategy_config)

        # Recuperer les overrides specifiques a la strategie
        overrides = asset_config.get("overrides", {}).get(strategy_name, {})

        if not overrides:
            self.logger.debug(f"Pas d'overrides pour la strategie '{strategy_name}'")
            # Meme sans overrides, on peut avoir des entry_rules specifiques a l'actif
            asset_entry_rules = asset_config.get("entry_rules", {})
            if asset_entry_rules:
                merged["entry_rules"] = self._deep_merge(
                    merged.get("entry_rules", {}), asset_entry_rules
                )
                self.logger.debug(
                    "[MERGE] entry_rules de l'actif fusionne (sans overrides)"
                )
            return merged

        self.logger.info(
            f"[MERGE] Application des overrides pour {strategy_name}: {list(overrides.keys())}"
        )

        # 1. Handle entry_rules from overrides (structure imbriquee)
        if "entry_rules" in overrides:
            override_entry = overrides["entry_rules"]
            merged_entry = merged.get("entry_rules", {})
            if strategy_name in merged_entry and strategy_name in override_entry:
                merged_entry[strategy_name] = self._deep_merge(
                    merged_entry[strategy_name], override_entry[strategy_name]
                )
            elif strategy_name in override_entry:
                merged_entry[strategy_name] = copy.deepcopy(override_entry[strategy_name])
            merged["entry_rules"] = merged_entry

        # 2. Ensure burst_scalping path exists
        burst_scalping = (
            merged.setdefault("entry_rules", {})
            .setdefault(strategy_name, {})
            .setdefault("burst_scalping", {})
        )

        # 3. TOUTES les autres sections -> deep merge dans burst_scalping
        #    (closure_rules, sltp, footprint, orderflow_v6, timing_gatekeeper, etc.)
        for key, value in overrides.items():
            if key == "entry_rules":
                continue  # Deja traite au-dessus
            if key in burst_scalping and isinstance(burst_scalping[key], dict) and isinstance(value, dict):
                burst_scalping[key] = self._deep_merge(burst_scalping[key], value)
            else:
                burst_scalping[key] = copy.deepcopy(value)

        # 4. Merge root entry_rules de l'asset config (ex: NAS100 avec entry_rules racine)
        asset_entry_rules = asset_config.get("entry_rules", {})
        if asset_entry_rules:
            merged["entry_rules"] = self._deep_merge(
                merged.get("entry_rules", {}), asset_entry_rules
            )

        # 5. Validation closure_rules (s'assurer que les defaults sont presents)
        closure = burst_scalping.get("closure_rules", {})
        if closure:
            self._ensure_closure_defaults(closure)

        # Log final
        self.logger.info("[MERGE_FINAL] Structure fusionnee:")
        if "entry_rules" in merged and strategy_name in merged["entry_rules"]:
            strat_config = merged["entry_rules"][strategy_name]
            if "burst_scalping" in strat_config:
                burst_keys = list(strat_config["burst_scalping"].keys())
                self.logger.info(f"  burst_scalping sections: {burst_keys}")
                cr = strat_config["burst_scalping"].get("closure_rules", {})
                if cr:
                    self.logger.info(
                        f"  closure_rules.target_profit_pips={cr.get('target_profit_pips')}, "
                        f"max_loss_pips={cr.get('max_loss_pips')}"
                    )

        return merged

    def _get_default_closure_rules(self) -> Dict[str, Any]:
        """
        Retourne les valeurs par defaut ABSOLUES pour closure_rules.
        """
        return {
            "enabled": True,
            "enable_profit_close": True,
            "enable_loss_guard": True,
            "target_profit_pips": 15.0,
            "max_loss_pips": 15.0,
            "require_full_count_for_profit_close": True,
            "require_all_seen_green_once": False,
            "all_seen_green_pips": 3.0,
            "min_green_pnl_pips": 0.0,
            "rt_fast_window_ms": 0,
            "rt_poll_interval_ms": 120,
            "loss_guard_arming_ms": 3000,
            "min_age_ms_for_any_close": 3000,
        }

    def _inject_asset_metadata(
        self, merged: Dict[str, Any], asset_config: Dict[str, Any]
    ) -> Dict[str, Any]:
        """
        Injecte les metadonnees de l'actif dans la config fusionnee.

        Metadonnees: symbol, type, symbol_info, volatility, risk_management, etc.
        NOTE: N'injecte PAS entry_rules ici (deja gere dans _merge_configs).
        """
        # Liste des cles de metadonnees a injecter
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

        # Injecter symbol_info directement au niveau racine aussi (compatibilite)
        if "symbol_info" in asset_config:
            merged["symbol_info"] = copy.deepcopy(asset_config["symbol_info"])

        return merged

    def _deep_merge(
        self, base: Dict[str, Any], override: Dict[str, Any]
    ) -> Dict[str, Any]:
        """
        Fusionne recursivement deux dictionnaires.
        Les valeurs de `override` ecrasent celles de `base`.
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
        Vide le cache des configs fusionnees.

        Args:
            asset_symbol: Si fourni, vide uniquement le cache pour cet actif.
                         Si None, vide tout le cache.
        """
        if asset_symbol:
            asset_upper = asset_symbol.upper()
            keys_to_remove = [
                k for k in self._merged_cache if k.startswith(asset_upper)
            ]
            for key in keys_to_remove:
                del self._merged_cache[key]
            self.logger.info(
                f"Cache vide pour {asset_symbol} ({len(keys_to_remove)} entrees)"
            )
        else:
            count = len(self._merged_cache)
            self._merged_cache.clear()
            self.logger.info(f"Cache entierement vide ({count} entrees)")

    def get_sltp_config(
        self, asset_symbol: str, strategy_name: str = "scalping"
    ) -> Dict[str, Any]:
        """
        Raccourci pour obtenir la config SLTP fusionnee pour un actif.

        Cette methode simplifie l'acces aux parametres SL/TP en cherchant
        dans l'ordre de priorite correct.

        Returns:
            Dict avec les cles: sl_pips, tp_pips, sl_method, tp_method, rr_base, etc.
        """
        merged = self.get_merged_config(asset_symbol, strategy_name)

        # Chemins possibles pour sltp (ordre de priorite)
        sltp_paths = [
            # 1. entry_rules.scalping.burst_scalping.sltp (priorite haute - asset override)
            ["entry_rules", strategy_name, "burst_scalping", "sltp"],
            # 2. overrides.scalping.sltp (dans asset config)
            ["overrides", strategy_name, "sltp"],
            # 3. entry_rules.scalping.sltp (fallback)
            ["entry_rules", strategy_name, "sltp"],
        ]

        for path in sltp_paths:
            sltp = self._get_nested(merged, path)
            if sltp and isinstance(sltp, dict) and sltp.get("sl"):
                self.logger.debug(f"[SLTP] Trouve dans {'.'.join(path)}")
                return self._flatten_sltp(sltp)

        self.logger.warning(f"[SLTP] Aucune config SLTP trouvee pour {asset_symbol}")
        return {}

    def _get_nested(self, data: Dict[str, Any], path: List[str]) -> Any:
        """Recupere une valeur imbriquee dans un dict."""
        current = data
        for key in path:
            if isinstance(current, dict) and key in current:
                current = current[key]
            else:
                return None
        return current

    def _flatten_sltp(self, sltp: Dict[str, Any]) -> Dict[str, Any]:
        """
        Aplatit la config SLTP pour un acces facile.

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
            result["min_sl_tp_distance_pips"] = (tp.get("execution", {}) or {}).get(
                "min_sl_tp_distance_pips"
            )

        # Autres parametres
        result["sl_method"] = sltp.get("sl_method")
        result["tp_method"] = sltp.get("tp_method")
        result["exit_mode"] = sltp.get("exit_mode")
        result["rr_base"] = sltp.get("rr_base")
        result["rr_floor"] = sltp.get("rr_floor")
        result["rr_cap"] = sltp.get("rr_cap")

        # Nettoyer les None
        result = {k: v for k, v in result.items() if v is not None}

        return result

    def get_closure_rules_config(
        self, asset_symbol: str, strategy_name: str = "scalping"
    ) -> Dict[str, Any]:
        """
        Raccourci pour obtenir la config closure_rules fusionnee pour un actif.

        Cherche dans entry_rules.{strategy_name}.burst_scalping.closure_rules
        (PAS a la racine).

        Retourne TOUJOURS un dict avec des valeurs par defaut si certaines cles manquent.
        """
        merged = self.get_merged_config(asset_symbol, strategy_name)

        # 1. Chercher dans entry_rules.scalping.burst_scalping.closure_rules
        closure_rules = (
            merged.get("entry_rules", {})
            .get(strategy_name, {})
            .get("burst_scalping", {})
            .get("closure_rules", {})
        )

        if closure_rules:
            self.logger.debug(
                f"[CLOSURE_RULES] {asset_symbol}: Trouve dans burst_scalping, "
                f"target_profit={closure_rules.get('target_profit_pips')}p"
            )
            return self._ensure_closure_defaults(copy.deepcopy(closure_rules))

        # 2. Si pas dans burst_scalping, charger depuis l'actif directement
        asset_config = self._load_asset_config(asset_symbol)
        if asset_config:
            asset_closure = (
                asset_config.get("overrides", {})
                .get(strategy_name, {})
                .get("closure_rules", {})
            )

            if asset_closure:
                self.logger.debug(
                    f"[CLOSURE_RULES] {asset_symbol}: Trouve dans overrides.scalping, "
                    f"target_profit={asset_closure.get('target_profit_pips')}p"
                )
                return self._ensure_closure_defaults(copy.deepcopy(asset_closure))

        # 3. Fallback: verifier dans la config de base
        strategy_config = self._load_strategy_base(strategy_name)
        if strategy_config:
            base_closure = (
                strategy_config.get("entry_rules", {})
                .get(strategy_name, {})
                .get("burst_scalping", {})
                .get("closure_rules", {})
            )

            if base_closure:
                self.logger.debug(
                    f"[CLOSURE_RULES] {asset_symbol}: Utilisation config de base, "
                    f"target_profit={base_closure.get('target_profit_pips')}p"
                )
                return self._ensure_closure_defaults(copy.deepcopy(base_closure))

        # 4. Dernier recours: valeurs par defaut ABSOLUES
        self.logger.warning(
            f"[CLOSURE_RULES] Aucune config trouvee pour {asset_symbol}, utilisation defaults"
        )
        return self._get_default_closure_rules()

    def _ensure_closure_defaults(self, closure_rules: Dict[str, Any]) -> Dict[str, Any]:
        """
        Garantit que toutes les cles necessaires sont presentes dans closure_rules.
        """
        defaults = self._get_default_closure_rules()

        # Fusionner, en gardant les valeurs de closure_rules si presentes
        for key, default_value in defaults.items():
            if key not in closure_rules:
                closure_rules[key] = default_value

        return closure_rules

    def list_available_assets(self) -> List[str]:
        """Liste tous les actifs ayant une config disponible."""
        assets_dir = Path(__file__).parent.parent / self.ASSETS_CONFIG_DIR
        if not assets_dir.is_dir():
            return []

        return [f.stem for f in assets_dir.glob("*.json")]

    def validate_asset_config(self, asset_symbol: str) -> Dict[str, Any]:
        """
        Valide la config d'un actif et retourne un rapport.

        Utile pour diagnostiquer les problemes de configuration.
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

        # Verifications
        required_keys = ["symbol", "symbol_info"]
        for key in required_keys:
            if key not in asset_config:
                report["warnings"].append(f"Cle '{key}' manquante")

        # Verifier symbol_info
        symbol_info = asset_config.get("symbol_info", {})
        required_symbol_info = ["point", "digits"]
        for key in required_symbol_info:
            if key not in symbol_info:
                report["errors"].append(f"symbol_info.{key} manquant (CRITIQUE)")
                report["valid"] = False

        # Verifier les overrides scalping
        scalping_overrides = asset_config.get("overrides", {}).get("scalping", {})
        if not scalping_overrides:
            report["warnings"].append("Pas d'overrides scalping definis")

        # Verifier closure_rules
        closure_rules = scalping_overrides.get("closure_rules", {})
        if not closure_rules:
            report["warnings"].append(
                "Pas de closure_rules definis dans overrides.scalping"
            )
        else:
            if not closure_rules.get("target_profit_pips"):
                report["warnings"].append(
                    "target_profit_pips manquant dans closure_rules"
                )
            if not closure_rules.get("max_loss_pips"):
                report["warnings"].append("max_loss_pips manquant dans closure_rules")

        # Verifier sltp
        sltp = self.get_sltp_config(asset_symbol)
        if not sltp.get("sl_pips"):
            report["warnings"].append("sl_pips non defini dans SLTP")
        if not sltp.get("tp_pips"):
            report["warnings"].append("tp_pips non defini dans SLTP")

        return report
