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
            
    def debug_merge_process(self, asset_symbol: str, strategy_name: str = "scalping"):
        """
        Debug complet du processus de fusion.
        """
        self.logger.info("=" * 80)
        self.logger.info(f"🔍 [DEBUG_MERGE] {asset_symbol} / {strategy_name}")
        self.logger.info("=" * 80)
        
        # 1. Charger les configs séparément
        strategy_config = self._load_strategy_base(strategy_name)
        asset_config = self._load_asset_config(asset_symbol)
        
        self.logger.info("📁 CONFIG DE BASE DE LA STRATÉGIE:")
        if strategy_config:
            if "closure_rules" in strategy_config:
                self.logger.info(f"  ✅ closure_rules: {strategy_config['closure_rules']}")
            else:
                self.logger.info("  ❌ closure_rules: ABSENT")
            
            # Chercher dans entry_rules.scalping.burst_scalping.closure_rules
            try:
                base_closure = (
                    strategy_config.get("entry_rules", {})
                    .get(strategy_name, {})
                    .get("burst_scalping", {})
                    .get("closure_rules", {})
                )
                if base_closure:
                    self.logger.info(f"  📍 closure_rules dans burst_scalping: {base_closure}")
            except:
                pass
        else:
            self.logger.error("  ❌ Impossible de charger la config de base")
        
        self.logger.info("")
        self.logger.info("📁 CONFIG DE L'ACTIF:")
        if asset_config:
            self.logger.info(f"  ✅ Config chargée pour {asset_symbol}")
            
            # Chercher overrides.scalping.closure_rules
            overrides = asset_config.get("overrides", {}).get(strategy_name, {})
            if "closure_rules" in overrides:
                self.logger.info(f"  ✅ closure_rules dans overrides: {overrides['closure_rules']}")
            else:
                self.logger.info(f"  ❌ closure_rules PAS dans overrides.{strategy_name}")
                self.logger.info(f"  📋 Overrides disponibles: {list(overrides.keys())}")
        else:
            self.logger.warning(f"  ⚠️ Pas de config spécifique pour {asset_symbol}")
        
        self.logger.info("")
        self.logger.info("🔄 PROCESSUS DE FUSION:")
        
        # 2. Simuler la fusion
        merged = self._merge_configs(strategy_config or {}, asset_config or {}, strategy_name)
        
        self.logger.info("")
        self.logger.info("✅ CONFIG FUSIONNÉE FINALE:")
        if "closure_rules" in merged:
            closure = merged["closure_rules"]
            self.logger.info(f"  ✅ closure_rules au niveau racine:")
            for k, v in closure.items():
                self.logger.info(f"     {k}: {v}")
        else:
            self.logger.error("  ❌ closure_rules ABSENT de la config fusionnée!")
        
        self.logger.info("=" * 80)   

    def get_merged_config(
        self,
        asset_symbol: str,
        strategy_name: str = "scalping",
        force_reload: bool = False,
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

        self.logger.info(
            f"[MERGE] Fusion config pour {asset_symbol} / {strategy_name}..."
        )

        # 1. Charger la config de base de la stratégie
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
                f"Pas de config spécifique pour {asset_symbol}, utilisation de la config de base"
            )
            self._merged_cache[cache_key] = strategy_config
            return copy.deepcopy(strategy_config)

        # 3. Fusionner les configs
        merged = self._merge_configs(strategy_config, asset_config, strategy_name)

        # 4. Ajouter les métadonnées de l'actif
        merged = self._inject_asset_metadata(merged, asset_config)

        # 5. Mettre en cache
        self._merged_cache[cache_key] = merged

        self.logger.info(
            f"[MERGE OK] Config fusionnée pour {asset_symbol}/{strategy_name}"
        )
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
            self.logger.debug(
                f"Config stratégie '{strategy_name}' chargée depuis {config_path}"
            )
            return config
        except Exception as e:
            self.logger.error(
                f"Erreur chargement config stratégie '{strategy_name}': {e}"
            )
            return {}

    def _load_asset_config(self, asset_symbol: str) -> Dict[str, Any]:
        """Charge la config spécifique d'un actif."""
        config_path = (
            Path(__file__).parent.parent
            / self.ASSETS_CONFIG_DIR
            / f"{asset_symbol}.json"
        )

        if not config_path.is_file():
            self.logger.debug(f"Pas de config spécifique pour {asset_symbol}")
            return {}

        try:
            config = self.config_loader.parse_json_config(str(config_path))
            self.logger.debug(
                f"Config actif '{asset_symbol}' chargée depuis {config_path}"
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
        Fusionne la config de stratégie avec les overrides de l'actif.

        Les overrides de l'actif (asset_config['overrides']['scalping']) sont appliqués
        sur les chemins correspondants dans la config de stratégie.

        Priorité de fusion (du plus faible au plus fort):
        1. Config de base de la stratégie
        2. Overrides de l'actif (dans asset_config['overrides'][strategy_name])
        3. SLTP de l'actif (dans asset_config['entry_rules'])

        Special handling pour closure_rules:
        - Est déplacé au niveau racine pour un accès facile
        """
        merged = copy.deepcopy(strategy_config)

        # Récupérer les overrides spécifiques à la stratégie
        overrides = asset_config.get("overrides", {}).get(strategy_name, {})

        if not overrides:
            self.logger.debug(f"Pas d'overrides pour la stratégie '{strategy_name}'")
            # Même sans overrides, on peut avoir des entry_rules spécifiques à l'actif
            asset_entry_rules = asset_config.get("entry_rules", {})
            if asset_entry_rules:
                merged["entry_rules"] = self._deep_merge(
                    merged.get("entry_rules", {}), asset_entry_rules
                )
                self.logger.debug(
                    "[MERGE] entry_rules de l'actif fusionné (sans overrides)"
                )
            return merged

        self.logger.info(
            f"[MERGE] Application des overrides pour {strategy_name}: {list(overrides.keys())}"
        )

        # ============================================================================
        # 🔥 CRITIQUE: Gestion SPÉCIALE de closure_rules - DOIT ÊTRE EN PREMIER
        # ============================================================================
        if "closure_rules" in overrides:
            closure_override = overrides["closure_rules"]

            # Log détaillé de ce qu'on reçoit
            self.logger.info("=" * 60)
            self.logger.info(f"🎯 [CLOSURE_RULES_OVERRIDE] {strategy_name}")
            self.logger.info(f"   Source: overrides.{strategy_name}.closure_rules")
            self.logger.info(f"   Valeurs reçues: {closure_override}")

            # Si closure_rules existe déjà dans la config de base, on fusionne
            existing_closure = merged.get("closure_rules", {})

            # Fusion profonde
            merged["closure_rules"] = self._deep_merge(
                existing_closure, closure_override
            )

            # Log de confirmation
            final_closure = merged["closure_rules"]
            self.logger.info(f"✅ closure_rules fusionné au niveau racine:")
            self.logger.info(f"   • enabled: {final_closure.get('enabled')}")
            self.logger.info(
                f"   • target_profit_pips: {final_closure.get('target_profit_pips')}p"
            )
            self.logger.info(
                f"   • max_loss_pips: {final_closure.get('max_loss_pips')}p"
            )
            self.logger.info(
                f"   • enable_profit_close: {final_closure.get('enable_profit_close')}"
            )
            self.logger.info(
                f"   • enable_loss_guard: {final_closure.get('enable_loss_guard')}"
            )
            self.logger.info("=" * 60)

            # Supprimer closure_rules des overrides pour éviter une double application
            # mais on garde une copie pour référence
            closure_override_backup = copy.deepcopy(closure_override)
        else:
            self.logger.warning(
                f"⚠️ [CLOSURE_RULES] Pas de closure_rules dans overrides.{strategy_name}"
            )
            closure_override_backup = None

        # ============================================================================
        # Gestion de entry_rules dans overrides (doit remplacer entry_rules.strategy_name)
        # ============================================================================
        if "entry_rules" in overrides:
            asset_entry_rules_override = overrides["entry_rules"]
            merged_entry_rules = merged.get("entry_rules", {})

            # Fusionner entry_rules spécifiques à la stratégie
            if strategy_name in merged_entry_rules:
                # Fusionner les entry_rules de la stratégie
                merged_entry_rules[strategy_name] = self._deep_merge(
                    merged_entry_rules[strategy_name], asset_entry_rules_override
                )
            else:
                # Créer une nouvelle entrée
                merged_entry_rules[strategy_name] = copy.deepcopy(
                    asset_entry_rules_override
                )

            merged["entry_rules"] = merged_entry_rules
            self.logger.debug(f"[OVERRIDE] entry_rules fusionné pour {strategy_name}")

        # ============================================================================
        # Gestion des autres overrides (sltp, burst_scalping, etc.)
        # Ils doivent aller dans entry_rules.strategy_name.burst_scalping
        # ============================================================================
        # Construire le chemin vers burst_scalping
        if "entry_rules" not in merged:
            merged["entry_rules"] = {}

        if strategy_name not in merged["entry_rules"]:
            merged["entry_rules"][strategy_name] = {}

        if "burst_scalping" not in merged["entry_rules"][strategy_name]:
            merged["entry_rules"][strategy_name]["burst_scalping"] = {}

        burst_scalping = merged["entry_rules"][strategy_name]["burst_scalping"]

        # Liste des sections spéciales déjà traitées
        already_processed = ["closure_rules", "entry_rules"]

        # Appliquer les autres overrides dans burst_scalping
        for section_key, section_value in overrides.items():
            if section_key in already_processed:
                continue  # Déjà traité

            if section_key in burst_scalping:
                # Deep merge pour les sections existantes
                burst_scalping[section_key] = self._deep_merge(
                    burst_scalping[section_key], section_value
                )
                self.logger.debug(
                    f"[OVERRIDE] {section_key} fusionné dans burst_scalping"
                )
            else:
                # Ajout direct pour les nouvelles sections
                burst_scalping[section_key] = copy.deepcopy(section_value)
                self.logger.debug(f"[OVERRIDE] {section_key} ajouté à burst_scalping")

        # Mettre à jour la structure
        merged["entry_rules"][strategy_name]["burst_scalping"] = burst_scalping

        # ============================================================================
        # Gestion des entry_rules directement dans asset_config (sltp principalement)
        # ============================================================================
        asset_entry_rules = asset_config.get("entry_rules", {})
        if asset_entry_rules:
            # Fusionner avec les entry_rules existants
            merged["entry_rules"] = self._deep_merge(
                merged.get("entry_rules", {}), asset_entry_rules
            )
            self.logger.debug("[ASSET_ENTRY_RULES] entry_rules de l'actif fusionné")

        # ============================================================================
        # Vérification finale et log
        # ============================================================================
        # Vérifier que closure_rules est bien présent au niveau racine
        if "closure_rules" not in merged:
            self.logger.warning(
                "⚠️ closure_rules absent de la config fusionnée, création avec valeurs par défaut"
            )
            merged["closure_rules"] = self._get_default_closure_rules()
        else:
            # S'assurer que les valeurs critiques sont présentes
            closure = merged["closure_rules"]
            if closure_override_backup:
                # Si on avait un override, vérifier que les valeurs sont bien présentes
                if (
                    "target_profit_pips" not in closure
                    and "target_profit_pips" in closure_override_backup
                ):
                    closure["target_profit_pips"] = closure_override_backup[
                        "target_profit_pips"
                    ]
                    self.logger.warning(
                        f"⚠️ target_profit_pips restauré depuis backup: {closure['target_profit_pips']}"
                    )

                if (
                    "max_loss_pips" not in closure
                    and "max_loss_pips" in closure_override_backup
                ):
                    closure["max_loss_pips"] = closure_override_backup["max_loss_pips"]
                    self.logger.warning(
                        f"⚠️ max_loss_pips restauré depuis backup: {closure['max_loss_pips']}"
                    )

            # Vérifier les valeurs minimales
            if closure.get("target_profit_pips", 0) <= 0:
                self.logger.error(
                    f"❌ target_profit_pips invalide: {closure.get('target_profit_pips')}"
                )
                closure["target_profit_pips"] = 15.0  # Fallback

            if closure.get("max_loss_pips", 0) <= 0:
                self.logger.error(
                    f"❌ max_loss_pips invalide: {closure.get('max_loss_pips')}"
                )
                closure["max_loss_pips"] = 15.0  # Fallback

        # Log final de la structure
        self.logger.info("[MERGE_FINAL] Structure fusionnée:")
        self.logger.info(
            f"  • closure_rules au niveau racine: {'✅' if 'closure_rules' in merged else '❌'}"
        )

        if "entry_rules" in merged and strategy_name in merged["entry_rules"]:
            strat_config = merged["entry_rules"][strategy_name]
            self.logger.info(
                f"  • entry_rules.{strategy_name}: {list(strat_config.keys())}"
            )

            if "burst_scalping" in strat_config:
                burst_keys = list(strat_config["burst_scalping"].keys())
                self.logger.info(f"  • burst_scalping sections: {burst_keys}")

        return merged

    def _get_default_closure_rules(self) -> Dict[str, Any]:
        """
        Retourne les valeurs par défaut ABSOLUES pour closure_rules.
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
            merged["entry_rules"] = self._deep_merge(
                merged_entry_rules, asset_entry_rules
            )
            self.logger.debug("[METADATA] entry_rules de l'actif injecté")

        return merged

    def _deep_merge(
        self, base: Dict[str, Any], override: Dict[str, Any]
    ) -> Dict[str, Any]:
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
            keys_to_remove = [
                k for k in self._merged_cache if k.startswith(asset_upper)
            ]
            for key in keys_to_remove:
                del self._merged_cache[key]
            self.logger.info(
                f"Cache vidé pour {asset_symbol} ({len(keys_to_remove)} entrées)"
            )
        else:
            count = len(self._merged_cache)
            self._merged_cache.clear()
            self.logger.info(f"Cache entièrement vidé ({count} entrées)")

    def get_sltp_config(
        self, asset_symbol: str, strategy_name: str = "scalping"
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
            result["min_sl_tp_distance_pips"] = (tp.get("execution", {}) or {}).get(
                "min_sl_tp_distance_pips"
            )

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

    def get_closure_rules_config(
        self, asset_symbol: str, strategy_name: str = "scalping"
    ) -> Dict[str, Any]:
        """
        Raccourci pour obtenir la config closure_rules fusionnée pour un actif.

        Retourne TOUJOURS un dict avec des valeurs par défaut si certaines clés manquent.
        """
        merged = self.get_merged_config(asset_symbol, strategy_name)

        # 1. Vérifier d'abord dans la config fusionnée au niveau racine
        closure_rules = merged.get("closure_rules", {})

        if closure_rules:
            self.logger.debug(
                f"[CLOSURE_RULES] {asset_symbol}: Trouvé au niveau racine, "
                f"target_profit={closure_rules.get('target_profit_pips')}p"
            )
            return self._ensure_closure_defaults(copy.deepcopy(closure_rules))

        # 2. Si pas au niveau racine, charger depuis l'actif
        asset_config = self._load_asset_config(asset_symbol)
        if asset_config:
            asset_closure = (
                asset_config.get("overrides", {})
                .get(strategy_name, {})
                .get("closure_rules", {})
            )

            if asset_closure:
                self.logger.debug(
                    f"[CLOSURE_RULES] {asset_symbol}: Trouvé dans overrides.scalping, "
                    f"target_profit={asset_closure.get('target_profit_pips')}p"
                )
                return self._ensure_closure_defaults(copy.deepcopy(asset_closure))

        # 3. Fallback: vérifier dans la config de base
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

        # 4. Dernier recours: valeurs par défaut ABSOLUES
        self.logger.warning(
            f"[CLOSURE_RULES] Aucune config trouvée pour {asset_symbol}, utilisation defaults"
        )
        return self._get_default_closure_rules()

    def _ensure_closure_defaults(self, closure_rules: Dict[str, Any]) -> Dict[str, Any]:
        """
        Garantit que toutes les clés nécessaires sont présentes dans closure_rules.
        """
        defaults = self._get_default_closure_rules()

        # Fusionner, en gardant les valeurs de closure_rules si présentes
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

        # Vérifier closure_rules
        closure_rules = scalping_overrides.get("closure_rules", {})
        if not closure_rules:
            report["warnings"].append(
                "Pas de closure_rules définis dans overrides.scalping"
            )
        else:
            if not closure_rules.get("target_profit_pips"):
                report["warnings"].append(
                    "target_profit_pips manquant dans closure_rules"
                )
            if not closure_rules.get("max_loss_pips"):
                report["warnings"].append("max_loss_pips manquant dans closure_rules")

        # Vérifier sltp
        sltp = self.get_sltp_config(asset_symbol)
        if not sltp.get("sl_pips"):
            report["warnings"].append("sl_pips non défini dans SLTP")
        if not sltp.get("tp_pips"):
            report["warnings"].append("tp_pips non défini dans SLTP")

        return report
