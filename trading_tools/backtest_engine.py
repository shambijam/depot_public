import pandas as pd
from datetime import datetime, UTC, timedelta
import logging
from pathlib import Path
from typing import Dict, Any, List, Optional
import sys
import os
import uuid  # Pour générer des IDs de trade virtuels

# --- DÉBUT DU BLOC DE CHEMIN PYTHON (IMPORTANT POUR LES IMPORTS) ---
# Ajoute la racine du projet au sys.path pour permettre les imports relatifs
# Cela permet d'importer des modules comme 'config_manager' ou 'phase_observer'
# qui sont à la racine ou dans des dossiers frères.
project_root = (
    Path(__file__).resolve().parents[1]
)
if str(project_root) not in sys.path:
    sys.path.insert(0, str(project_root))
# --- FIN DU BLOC DE CHEMIN PYTHON ---

# Imports des modules du projet nécessaires
try:
    from core.config_manager import ConfigManager
    from utils.data_preprocessing import load_and_preprocess_dukascopy_csv
    from phase_observer.phase_observer import PhaseObserver
    from strategy.base_strategy import (
        BaseStrategy,
    )

except ImportError as e:
    logging.critical(
        f"ERREUR FATALE: Échec de l'importation d'un module essentiel pour le BacktestEngine. Erreur: {e}"
    )


logger = logging.getLogger(__name__)


class BacktestEngine:
    """
    Moteur de backtest complet pour SNIPER_X.
    Il simule le comportement du bot sur des données historiques pour évaluer les stratégies.
    """

    def __init__(self, config_manager: ConfigManager):
        """
        Initialise le moteur de backtest.

        Args:
            config_manager (ConfigManager): L'instance du gestionnaire de configuration.
        """
        self.config_manager = config_manager

        # Initialisation des briques clés pour la simulation
        self.phase_observer = PhaseObserver(config_manager=self.config_manager)

        # Pour le backtest, nous aurons un état interne des positions et du capital simulés
        self.simulated_account_equity = self.config_manager.get(
            "risk_management_settings.default_account_equity", 10000.0
        )
        self.initial_equity = self.simulated_account_equity
        self.simulated_positions = {}
        self.simulated_closed_trades = []
        self.peak_equity = self.simulated_account_equity
        self.max_drawdown = 0.0

        # Ces snapshots temporaires sont vidés à chaque exécution de run_backtest
        self.all_processed_data: Dict[str, pd.DataFrame] = (
            {}
        )
        self.current_bar_data_snapshot: Dict[str, pd.Series] = (
            {}
        )
        self.simulated_asset_signals: Dict[str, Dict[str, Any]] = (
            {}
        )
        self.simulated_market_data_for_context: Dict[str, Dict[str, Any]] = (
            {}
        )

        logger.info("BacktestEngine initialisé.")

    def _get_nested_config_param(self, config_dict: Dict[str, Any], path: str, default: Any = None) -> Any:
        """
        Récupère une valeur de paramètre depuis un dictionnaire de configuration imbriqué
        en utilisant un chemin pointé (ex: 'section.sous_section.parametre').
        """
        parts = path.split('.')
        current_level = config_dict
        for part in parts:
            if isinstance(current_level, dict) and part in current_level:
                current_level = current_level[part]
            else:
                return default # Retourne la valeur par défaut si le chemin n'existe pas
        return current_level
   
    def run_backtest(
        self,
        symbols_to_backtest: Dict[
            str, str
        ],  # Ex: {"EURUSD": "EURUSD_Candlestick_1_M_BID_....csv", "GBPUSD": "GBPUSD_....csv"}
        timeframe: str,
        start_date: Optional[
            datetime
        ] = None,  # Pour la gestion future de la plage de dates
        end_date: Optional[datetime] = None,
        strategy_config_override: Optional[Dict[str, Any]] = None # Nouveau paramètre
    ) -> Dict[str, Any]:
        """
        Lance un backtest complet sur les symboles spécifiés.

        Args:
            symbols_to_backtest (Dict[str, str]): Dictionnaire où la clé est le symbole (ex: "EURUSD")
                                                et la valeur est le nom du fichier CSV correspondant.
            timeframe (str): Le timeframe des données (ex: "M1", "H1").
            start_date (Optional[datetime]): Date de début du backtest. Si None, début du fichier.
            end_date (Optional[datetime]): Date de fin du backtest. Si None, fin du fichier.
            strategy_config_override (Optional[Dict[str, Any]]): Surcharge la configuration dynamique du ConfigManager
                                                                    pour ce backtest spécifique. Utilisé par l'Optimizer.

        Returns:
            Dict[str, Any]: Un dictionnaire des résultats globaux du backtest.
        """
        logger.info("Démarrage du backtest...")

        self.all_processed_data = (
            {}
        )

        # Si une surcharge est fournie (par l'Optimizer), nous l'utiliserons comme config principale
        # Sinon, nous utilisons la config dynamique actuelle du ConfigManager global.
        effective_config = strategy_config_override if strategy_config_override is not None else self.config_manager.get_current_dynamic_config()


        for symbol, file_name in symbols_to_backtest.items():
            file_path = Path("data") / "raw_historical" / file_name
            if not file_path.exists():
                logger.error(
                    f"Fichier historique manquant pour {symbol} à {file_path}. Ce symbole sera ignoré."
                )
                continue

            # Récupérer les paramètres spécifiques à l'actif depuis la effective_config
            asset_config = self._get_nested_config_param(effective_config, f"asset_configs.{symbol}", {})
            
            # Déterminer les valeurs de spread et point en fonction du symbole et des configs d'actif
            symbol_spread_pips = 0.5 # Default FX
            symbol_point_value = 0.00001 # Default FX

            if "USD" in symbol.upper() and (
                "BTC" in symbol.upper()
                or "ETH" in symbol.upper()
                or "SOL" in symbol.upper()
                or "LTC" in symbol.upper()
            ):
                symbol_spread_pips = asset_config.get("spread_analysis", {}).get(
                    "expected_average_spread_pips", 50.0
                )
                symbol_point_value = asset_config.get("point", 0.01)
            elif "JPY" in symbol.upper():
                symbol_spread_pips = asset_config.get("spread_analysis", {}).get(
                    "expected_average_spread_pips", 0.8
                )
                symbol_point_value = asset_config.get("point", 0.001)
            elif "XAU" in symbol.upper():
                symbol_spread_pips = asset_config.get("spread_analysis", {}).get(
                    "expected_average_spread_pips", 30.0
                )
                symbol_point_value = asset_config.get("point", 0.01)

            if symbol_point_value <= 0:
                symbol_point_value = 0.00001 if "JPY" not in symbol.upper() else 0.001
            if symbol_spread_pips <= 0:
                symbol_spread_pips = 0.5

            processed_df = load_and_preprocess_dukascopy_csv(
                file_path,
                symbol=symbol,
                timeframe=timeframe,
                estimated_spread_pips=symbol_spread_pips,
                fixed_point_value=symbol_point_value,
            )

            if not processed_df.empty:
                if start_date:
                    processed_df = processed_df[processed_df.index >= start_date]
                if end_date:
                    processed_df = processed_df[processed_df.index <= end_date]

                if processed_df.empty:
                    logger.warning(
                        f"Aucune donnée dans la plage spécifiée pour {symbol}. Ignoré."
                    )
                    continue

                self.all_processed_data[symbol] = processed_df
                logger.info(
                    f"Données pour {symbol} prêtes pour le backtest ({len(processed_df)} barres)."
                )
            else:
                logger.error(f"Échec du prétraitement ou données vides pour {symbol}.")

        if not self.all_processed_data:
            logger.error(
                "Aucune donnée prétraitée disponible pour le backtest. Fin de l'exécution."
            )
            return {"error": "No data for backtest"}

        logger.info("Lancement de la simulation barre par barre...")

        all_timestamps = pd.DatetimeIndex([])
        for df_idx in self.all_processed_data.values():
            all_timestamps = all_timestamps.union(df_idx.index)
        all_timestamps = (
            all_timestamps.sort_values().tolist()
        )

        self.simulated_account_equity = effective_config.get(
            "risk_management_settings.default_account_equity", 10000.0
        )
        self.initial_equity = self.simulated_account_equity
        self.simulated_positions = {}
        self.simulated_closed_trades = []
        self.peak_equity = self.simulated_account_equity
        self.max_drawdown = 0.0

        self.current_bar_data_snapshot = {
            symbol: pd.Series(dtype="float64")
            for symbol in self.all_processed_data.keys()
        }
        self.simulated_asset_signals = {}
        self.simulated_market_data_for_context = {}

        total_simulated_trades = 0

        for current_timestamp in all_timestamps:
            active_symbols_in_this_bar_for_context = []

            for symbol, df_data_full in self.all_processed_data.items():
                if current_timestamp in df_data_full.index:
                    self.current_bar_data_snapshot[symbol] = df_data_full.loc[
                        current_timestamp
                    ]
                    active_symbols_in_this_bar_for_context.append(
                        symbol
                    )

                    df_up_to_current = df_data_full.loc[
                        df_data_full.index <= current_timestamp
                    ].copy()

                    if not df_up_to_current.empty:
                        lookback_window = effective_config.get(
                            "phase_detection_settings.lookback_window", 20
                        )
                        
                        if len(df_up_to_current) >= lookback_window:
                            df_to_analyze = df_up_to_current.iloc[-lookback_window:].copy()
                        else:
                            df_to_analyze = df_up_to_current.copy()

                        df_to_analyze_by_phase_observer = df_to_analyze.reset_index()
                        
                        if not df_to_analyze_by_phase_observer.empty:
                            
                            # Ajoute des colonnes avec des valeurs par défaut pour éviter le KeyError
                            # qui se produit si les signaux ne sont pas activés dans la configuration PhaseObserver
                            columns_to_add_or_ensure = [
                                "fvg_details", "ob_details", "bos_mss_details",
                                "liquidity_grab_details", "volume_anomaly_details",
                                "eqh_eql_details", "fvg_detected", "ob_detected",
                                "bos_mss_detected", "liquidity_grab_detected",
                                "volume_anomaly_detected", "eqh_eql_detected",
                                "trend", "volume_momentum", "nearest_liquidity_level_details",
                                "entry_confirmation_bullish", "entry_confirmation_bearish", "validated_ob"
                            ]
                            for col in columns_to_add_or_ensure:
                                if col not in df_to_analyze_by_phase_observer.columns:
                                    if col.endswith('_detected'):
                                        df_to_analyze_by_phase_observer[col] = False
                                    elif col.endswith('_details'):
                                        df_to_analyze_by_phase_observer[col] = pd.Series([None] * len(df_to_analyze_by_phase_observer), index=df_to_analyze_by_phase_observer.index, dtype=object)
                                    elif col in ["trend"]:
                                        df_to_analyze_by_phase_observer[col] = "neutral"
                                    elif col in ["volume_momentum"]:
                                        df_to_analyze_by_phase_observer[col] = 0.0
                                    elif col in ["spread", "point", "trade_tick_size", "trade_contract_size"]: 
                                        default_val = df_data_full.iloc[0].get(col)
                                        if pd.isna(default_val) or default_val == 0: 
                                            if col == 'point': default_val = 0.00001
                                            elif col == 'trade_tick_size': default_val = 0.00001
                                            elif col == 'trade_contract_size': default_val = 100000.0
                                            elif col == 'spread': default_val = 0.0 
                                        df_to_analyze_by_phase_observer[col] = default_val
                                    else: 
                                        df_to_analyze_by_phase_observer[col] = None
                            
                            for col in ["fvg_detected", "ob_detected", "bos_mss_detected", "liquidity_grab_detected", "volume_anomaly_detected", "eqh_eql_detected", "entry_confirmation_bullish", "entry_confirmation_bearish", "validated_ob"]:
                                if col in df_to_analyze_by_phase_observer.columns:
                                    df_to_analyze_by_phase_observer[col] = df_to_analyze_by_phase_observer[col].astype(bool)
                            
                            # Les paramètres de PhaseObserver viennent de effective_config
                            asset_config_for_phase = effective_config.get("asset_configs", {}).get(symbol, {})
                            if asset_config_for_phase and asset_config_for_phase.get("phase_detection"):
                                self.phase_observer.update_parameters_from_config(asset_config_for_phase)
                            else:
                                # Fallback si pas de config spécifique pour l'actif, utilise les defaults de PhaseObserver
                                self.phase_observer.update_parameters_from_config(effective_config.get("phase_detection_settings", {}))
                                
                            # --- DÉBUT DE LA NOUVELLE CORRECTION : TRY-EXCEPT POUR L'ANALYSE ---
                            try:
                                annotated_df = self.phase_observer.analyze(
                                    df_to_analyze_by_phase_observer
                                )
                            except Exception as e:
                                logger.error(f"Erreur lors de l'analyse PhaseObserver pour {symbol} à {current_timestamp.isoformat()}: {e}", exc_info=True)
                                annotated_df = None 
                            # --- FIN DE LA NOUVELLE CORRECTION ---
                            
                            if annotated_df is not None and not annotated_df.empty:
                                latest_signals_row = annotated_df.iloc[-1]

                                current_point = latest_signals_row.get("point", 0.00001)
                                current_spread_abs = latest_signals_row.get("spread", 0.0)

                                self.simulated_asset_signals[symbol] = {
                                    "confidence_score": latest_signals_row.get(
                                        "confidence_score", 0.0
                                    ),
                                    "phase": latest_signals_row.get("phase", "unknown"),
                                    "volume_momentum": latest_signals_row.get(
                                        "volume_momentum", 0.0
                                    ),
                                    "is_liquid": latest_signals_row.get("is_liquid", True),
                                    "current_price": latest_signals_row.get("close"),
                                    "current_spread_points": (
                                        current_spread_abs / current_point
                                        if current_point > 0
                                        else 0.0
                                    ),
                                    "symbol_point_value": current_point,
                                    "symbol_trade_contract_size": effective_config.get("asset_configs",{}).get(symbol,{}).get("contract_size", 1.0), # Charger du asset_config via effective_config
                                    "symbol_trade_tick_size": latest_signals_row.get(
                                        "trade_tick_size", 0.00001
                                    ),
                                    "fvg_detected": latest_signals_row.get(
                                        "fvg_detected", False
                                    ),
                                    "fvg_details": latest_signals_row.get("fvg_details"),
                                    "ob_detected": latest_signals_row.get("ob_detected", False),
                                    "ob_details": latest_signals_row.get("ob_details"),
                                    "bos_mss_detected": latest_signals_row.get(
                                        "bos_mss_detected", False
                                    ),
                                    "bos_mss_details": latest_signals_row.get(
                                        "bos_mss_details"
                                    ),
                                    "liquidity_grab_detected": latest_signals_row.get(
                                        "liquidity_grab_detected", False
                                    ),
                                    "liquidity_grab_details": latest_signals_row.get(
                                        "liquidity_grab_details"
                                    ),
                                    "eqh_eql_detected": latest_signals_row.get(
                                        "eqh_eql_detected", False
                                    ),
                                    "eqh_eql_details": latest_signals_row.get(
                                        "eqh_eql_details"
                                    ),
                                    "volume_anomaly_detected": latest_signals_row.get(
                                        "volume_anomaly_detected", False
                                    ),
                                    "volume_anomaly_details": latest_signals_row.get(
                                        "volume_anomaly_details"
                                    ),
                                    "nearest_liquidity_level_details": latest_signals_row.get(
                                        "nearest_liquidity_level_details"
                                    ),
                                    "entry_confirmation_bullish": latest_signals_row.get(
                                        "entry_confirmation_bullish", False
                                    ),
                                    "entry_confirmation_bearish": latest_signals_row.get(
                                        "entry_confirmation_bearish", False
                                    ),
                                    "validated_ob": latest_signals_row.get(
                                        "validated_ob", False
                                    ),
                                    "spread": current_spread_abs,
                                    "point": current_point,
                                }
                                self.simulated_market_data_for_context[symbol] = {
                                    "annotated_rates_df": annotated_df.copy(),
                                    "symbol_info": {
                                        "name": symbol,
                                        "point": current_point,
                                        "spread": current_spread_abs,
                                    },
                                }
                            else:
                                logger.warning(
                                    f"PhaseObserver n'a pas retourné de données annotées pour {symbol} à {current_timestamp.isoformat()}. Ignoré."
                                )
                                self.simulated_asset_signals[symbol] = {}
                                self.simulated_market_data_for_context[symbol] = {}

            if not any(self.simulated_asset_signals.values()):
                if current_timestamp: # S'assurer que le timestamp existe avant de l'utiliser
                    logger.debug(
                        f"No active signals for any symbol at {current_timestamp.isoformat()}. Skipping decision."
                    )
                continue

            current_context = {
                "market_data": self.simulated_market_data_for_context,
                "trading_signals": self.simulated_asset_signals,
                "account_info": {
                    "equity": self.simulated_account_equity,
                    "balance": self.simulated_account_equity,
                },
                "open_positions": list(
                    self.simulated_positions.values()
                ),
                "current_time_utc": current_timestamp,
                "active_broker_account": {
                    "account_id": "backtest_account",
                    "broker_name": "SIMULATED_BROKER",
                    "trade_settings": {
                        "min_lot": 0.01,
                        "max_lot": 50.0,
                        "lot_step": 0.01,
                    },
                },
                "economic_calendar": [],
                "vix_index": 0,
            }

            unrealized_pnl = 0.0
            for ticket, pos_data in self.simulated_positions.items():
                if (
                    pos_data["symbol"] in self.simulated_asset_signals
                    and "current_price"
                    in self.simulated_asset_signals[pos_data["symbol"]]
                ):
                    current_price = self.simulated_asset_signals[pos_data["symbol"]][
                        "current_price"
                    ]
                    pnl_per_unit = (
                        (current_price - pos_data["entry_price"])
                        if pos_data["type"] == 0
                        else (pos_data["entry_price"] - current_price)
                    )

                    asset_symbol_info_for_pnl = self.simulated_asset_signals.get(
                        pos_data["symbol"], {}
                    )

                    # AMÉLIORATION : Simplification du calcul du P&L non réalisé
                    contract_size_for_pnl = asset_symbol_info_for_pnl.get(
                        "symbol_trade_contract_size", 100000.0
                    )
                    unrealized_pnl += pnl_per_unit * contract_size_for_pnl


            current_virtual_equity = self.simulated_account_equity + unrealized_pnl
            self.peak_equity = max(self.peak_equity, current_virtual_equity)
            drawdown_percent = (
                ((self.peak_equity - current_virtual_equity) / self.peak_equity) * 100
                if self.peak_equity > 0
                else 0.0
            )
            self.max_drawdown = max(self.max_drawdown, drawdown_percent)

            # AMÉLIORATION : Utilisation de la configuration effective pour les décisions de sortie
            exit_decisions = self._decide_exit_trades_backtest(
                context=current_context,
                open_positions=current_context["open_positions"],
                effective_config=effective_config,
            )

            if exit_decisions:
                for exit_decision in exit_decisions:
                    ticket_to_close = exit_decision.get("ticket_to_close")
                    if ticket_to_close in self.simulated_positions:
                        pos_data = self.simulated_positions[ticket_to_close]
                        simulated_close_price = self.simulated_asset_signals[
                            pos_data["symbol"]
                        ]["current_price"]
                        self._simulate_close_position(
                            pos_data, simulated_close_price, current_timestamp
                        )
                    else:
                        logger.warning(
                            f"Attempted to close non-existent position {ticket_to_close}."
                        )

            strategies_to_test_for_entry = {}

            for asset_symbol_with_signals in active_symbols_in_this_bar_for_context:
                default_strategy_key = effective_config.get( # UTILISE effective_config
                    "strategies.default_strategy"
                )
                default_strategy_config = effective_config.get("strategies", {}).get( # UTILISE effective_config
                    default_strategy_key
                ) # Get the specific strategy config within 'strategies' section

                if not default_strategy_config:
                    # CORRECTION : Remplacement de 'asset_symbol' par 'asset_symbol_with_signals'
                    logger.warning(f"Default strategy config '{default_strategy_key}' not found in effective config. Skipping entry decision for {asset_symbol_with_signals}.")
                    continue # Skip if default strategy config isn't found

                # Load strategy specific config (from effective_config)
                # and pass it to PhaseObserver if it has phase_detection settings
                asset_config_for_phase = effective_config.get("asset_configs", {}).get(asset_symbol_with_signals, {})
                if asset_config_for_phase and asset_config_for_phase.get("phase_detection"):
                    self.phase_observer.update_parameters_from_config(asset_config_for_phase)
                else:
                    # Fallback si pas de config spécifique pour l'actif, utilise les defaults de PhaseObserver
                    self.phase_observer.update_parameters_from_config(effective_config.get("phase_detection_settings", {}))

                strategies_to_test_for_entry[asset_symbol_with_signals] = {
                    "strategy_name": default_strategy_config.get("strategy_name"),
                    "strategy_class": next(
                        (
                            s_data.get("strategy_class")
                            for s_data in self.config_manager._config_knowledge_base.values()
                            if s_data.get("content", {}).get("strategy_name")
                            == default_strategy_config.get("strategy_name")
                        ),
                        None,
                    ),
                    "adapted_config": default_strategy_config, # Ici la config est la spécifique à la stratégie (du fichier).
                }

            for asset_symbol, strategy_info in strategies_to_test_for_entry.items():
                if strategy_info is None or not strategy_info.get("strategy_class"):
                    continue

                strategy_class = strategy_info["strategy_class"]
                adapted_strategy_config = strategy_info["adapted_config"]

                strategy_instance = strategy_class(
                    config_manager_instance=self.config_manager,
                    strategy_config=adapted_strategy_config,
                )

                trade_decision = strategy_instance.evaluate_entry(
                    current_context, self.simulated_asset_signals
                )

                if trade_decision:
                    # Correction: il manquait un `asset_symbol` ici, j'ai utilisé `trade_decision['asset']`
                    if not self.simulated_asset_signals[trade_decision['asset']].get(
                        "is_liquid", False
                    ):
                        logger.debug(f"Skipping trade for {trade_decision['asset']}: not liquid.")
                        continue

                    self._simulate_open_trade(trade_decision, current_timestamp)
                    total_simulated_trades += 1
                    logger.debug(
                        f"Simulated open trade for {trade_decision['asset']} via {trade_decision['strategy_type']} at {current_timestamp.isoformat()}"
                    )

        logger.info("Simulation du backtest terminée (avec logique implémentée).")

        total_profit_usd = sum(
            trade["pnl_usd"] for trade in self.simulated_closed_trades
        )

        net_profit_percent = (
            (
                (self.simulated_account_equity - self.initial_equity)
                / self.initial_equity
            )
            * 100
            if self.initial_equity > 0
            else 0.0
        )

        global_results = {
            "total_trades_simulated": total_simulated_trades,
            "total_profit_usd": total_profit_usd,
            "net_profit_percent": net_profit_percent,
            "final_equity": self.simulated_account_equity,
            "max_drawdown_percent": self.max_drawdown,
            "closed_trades_details": self.simulated_closed_trades,
            "remaining_open_positions_count": len(self.simulated_positions),
        }
        logger.info("Backtest terminé. Résultats globaux : %s", global_results)
        return global_results


    # ===== PARTIE 2: MÉTHODE À AJOUTER À LA FIN DE LA CLASSE BacktestEngine =====
    # (Ajouter cette méthode complète juste avant "if __name__ == '__main__':")

    def _decide_exit_trades_backtest(
        self, 
        context: Dict[str, Any], 
        open_positions: List[Dict[str, Any]], 
        effective_config: Dict[str, Any]
    ) -> List[Dict[str, Any]]:
        """
        Logique de sortie simplifiée pour le backtest.
        Vérifie SL/TP et conditions de sortie basiques.
        """
        exit_decisions = []
        
        for position in open_positions:
            symbol = position["symbol"]
            entry_price = position["entry_price"]
            position_type = position["type"]  # 0=BUY, 1=SELL
            sl_price = position.get("sl", 0.0)
            tp_price = position.get("tp", 0.0)
            
            # Récupérer le prix actuel
            if symbol not in context["trading_signals"]:
                continue
                
            current_price = context["trading_signals"][symbol].get("current_price")
            if current_price is None:
                continue
            
            should_exit = False
            exit_reason = ""
            
            # Vérification Stop Loss
            if sl_price > 0:
                if position_type == 0:  # BUY position
                    if current_price <= sl_price:
                        should_exit = True
                        exit_reason = "stop_loss"
                else:  # SELL position
                    if current_price >= sl_price:
                        should_exit = True
                        exit_reason = "stop_loss"
            
            # Vérification Take Profit
            if tp_price > 0 and not should_exit:
                if position_type == 0:  # BUY position
                    if current_price >= tp_price:
                        should_exit = True
                        exit_reason = "take_profit"
                else:  # SELL position
                    if current_price <= tp_price:
                        should_exit = True
                        exit_reason = "take_profit"
            
            # Ajouter la décision de sortie
            if should_exit:
                exit_decisions.append({
                    "ticket_to_close": position["ticket"],
                    "close_price": current_price,
                    "exit_reason": exit_reason,
                    "symbol": symbol
                })
        
        return exit_decisions

    def _simulate_open_trade(self, trade_decision: Dict[str, Any], timestamp: datetime):
        """Simule l'ouverture d'un trade et met à jour l'état interne."""
        symbol = trade_decision["asset"]
        action = trade_decision["action"]
        volume = trade_decision["volume"]

        entry_price = trade_decision.get("entry_price")
        if entry_price is None:
            entry_price = self.simulated_asset_signals.get(symbol, {}).get(
                "current_price"
            )
            if entry_price is None:
                logger.warning(
                    f"Could not determine entry price for {symbol} at {timestamp}. Skipping trade."
                )
                return

        symbol_point_value = self.simulated_asset_signals.get(symbol, {}).get(
            "symbol_point_value", 0.00001
        )
        symbol_contract_size = self.simulated_asset_signals.get(symbol, {}).get(
            "symbol_trade_contract_size", 1.0
        )

        sl_pips = trade_decision.get("target_sl_pips", 0)
        tp_pips = trade_decision.get("target_tp_pips", 0)

        sl_price_abs = 0.0
        tp_price_abs = 0.0

        if action == "BUY":
            sl_price_abs = entry_price - (sl_pips * symbol_point_value)
            tp_price_abs = entry_price + (tp_pips * symbol_point_value)
        elif action == "SELL":
            sl_price_abs = entry_price + (sl_pips * symbol_point_value)
            tp_price_abs = entry_price - (tp_pips * symbol_point_value)

        spread_abs = self.simulated_asset_signals.get(symbol, {}).get("spread", 0.0)
        cost_of_spread = spread_abs * volume * symbol_contract_size

        if (
            sl_price_abs > 0
            and entry_price > 0
            and volume > 0
            and symbol_contract_size > 0
        ):
            sl_distance_abs = abs(entry_price - sl_price_abs)
            initial_risk_usd = sl_distance_abs * volume * symbol_contract_size
        else:
            initial_risk_usd = (
                self.simulated_account_equity
                * self.config_manager.get(
                    "global_safety.max_risk_per_trade_percent", 1.0
                )
                / 100
            )

        new_position = {
            "ticket": len(self.simulated_positions)
            + len(self.simulated_closed_trades)
            + 1,
            "symbol": symbol,
            "type": 0 if action == "BUY" else 1,
            "volume": volume,
            "entry_price": entry_price,
            "sl": sl_price_abs,
            "tp": tp_price_abs,
            "open_time": timestamp.isoformat(),
            "initial_risk_usd": initial_risk_usd,
            "strategy_type": trade_decision["strategy_type"],
            "rule_name": trade_decision["rule_name"],
            "magic": trade_decision["magic_number"],  # Correction: Utilise 'magic_number'
            "entry_signals_snapshot": self.simulated_asset_signals.get(symbol, {}),
        }
        self.simulated_positions[new_position["ticket"]] = new_position

        self.simulated_account_equity -= cost_of_spread

        logger.debug(
            f"Simulated OPEN: {action} {volume} {symbol} @ {entry_price:.5f} (SL:{new_position['sl']:.5f}, TP:{new_position['tp']:.5f}) Risk:{initial_risk_usd:.2f}$ Spread_Cost:{cost_of_spread:.2f}$"
        )


    def _simulate_close_position(
        self, position_data: Dict[str, Any], close_price: float, timestamp: datetime
    ):
        """Simule la clôture d'un trade et met à jour l'état interne et le P&L."""
        symbol = position_data["symbol"]
        entry_price = position_data["entry_price"]
        volume = position_data["volume"]
        action_type = position_data["type"]

        symbol_point_value = position_data["entry_signals_snapshot"].get(
            "symbol_point_value", 0.00001
        )
        symbol_contract_size = position_data["entry_signals_snapshot"].get(
            "symbol_trade_contract_size", 100000.0
        )

        spread_abs_exit = self.simulated_asset_signals.get(symbol, {}).get(
            "spread", 0.0
        )
        cost_of_spread_exit = spread_abs_exit * volume * symbol_contract_size

        pnl_per_unit = (
            (close_price - entry_price)
            if action_type == 0
            else (entry_price - close_price)
        )
        pnl_usd = pnl_per_unit * symbol_contract_size

        commission_per_lot = (
            self.config_manager.load_asset_config(symbol)
            .get("trade_settings", {})
            .get("commission_per_lot_usd", 0.0)
        )
        total_commission = commission_per_lot * volume
        pnl_usd -= total_commission

        closed_trade_info = {
            "ticket": position_data["ticket"],
            "symbol": symbol,
            "type": "BUY" if action_type == 0 else "SELL",
            "volume": volume,
            "entry_price": entry_price,
            "close_price": close_price,
            "pnl_usd": pnl_usd,
            "open_time": position_data["open_time"],
            "close_time": timestamp.isoformat(),
            "strategy_type": position_data["strategy_type"],
            "rule_name": position_data["rule_name"],
            "magic": position_data["magic"],
            "initial_risk_usd": position_data["initial_risk_usd"],
            "entry_signals_snapshot": position_data["entry_signals_snapshot"],
        }
        self.simulated_closed_trades.append(closed_trade_info)

        self.simulated_account_equity += pnl_usd

        self.peak_equity = max(self.peak_equity, self.simulated_account_equity)
        current_drawdown = (
            ((self.peak_equity - self.simulated_account_equity) / self.peak_equity)
            * 100
            if self.peak_equity > 0
            else 0.0
        )
        self.max_drawdown = max(self.max_drawdown, current_drawdown)

        del self.simulated_positions[position_data["ticket"]]

        logger.debug(
            f"Simulated CLOSE: {symbol} #{position_data['ticket']} @ {close_price:.5f} PnL: {pnl_usd:.2f}$ Equity: {self.simulated_account_equity:.2f}$"
        )



if __name__ == "__main__":
    logging.basicConfig(
        level=logging.DEBUG, format="%(asctime)s - %(levelname)s - %(message)s"
    )
    
    print("\n--- Démarrage du Test du BacktestEngine ---")

    try:
        prod_config_path = Path("config") / "prod_config.json"
        if not prod_config_path.exists():
            logger.error(
                f"FATAL: Le fichier prod_config.json est introuvable à '{prod_config_path}'."
            )
            logger.error(
                "Veuillez vous assurer que votre dossier 'config/' et 'prod_config.json' existent."
            )
            exit(1)

        config_manager_instance = ConfigManager()
        config_manager_instance.initialize_dynamic_config(
            template_path=str(prod_config_path),
            output_path=str(
                Path("output") / "dynamic_config_backtest_temp.json"
            ),
            config_dir=str(
                Path("config") / "strategy"
            ),
        )
        logger.info(
            "ConfigManager initialisé avec succès pour le test du BacktestEngine."
        )
    except Exception as e:
        logger.critical(
            f"Erreur critique lors de l'initialisation du ConfigManager : {e}",
            exc_info=True,
        )
        logger.critical("Impossible de continuer le test du BacktestEngine.")
        exit(1)

    backtest_engine = BacktestEngine(config_manager=config_manager_instance)

    symbols_for_test = {
        "EURUSD": "EURUSD_Candlestick_1_M_BID_01.04.2025-19.07.2025.csv",
        "GBPUSD": "GBPUSD_Candlestick_1_M_BID_01.04.2025-19.07.2025.csv",
        "USDCHF": "USDCHF_Candlestick_1_M_BID_01.04.2025-19.07.2025.csv",
        "XAUUSD": "XAUUSD_Candlestick_1_M_BID_01.04.2025-19.07.2025.csv",
        "BTCUSD": "BTCUSD_Candlestick_1_M_BID_01.04.2025-19.07.2025.csv",
        "ETHUSD": "ETHUSD_Candlestick_1_M_BID_01.04.2025-19.07.2025.csv",
        "LTCUSD": "LTCUSD_Candlestick_1_M_BID_01.04.2025-19.07.2025.csv",
    }

    test_timeframe = (
        "M1"
    )

    print("\n--- Appel de run_backtest pour charger les données ---")
    backtest_results = backtest_engine.run_backtest(
        symbols_to_backtest=symbols_for_test,
        timeframe=test_timeframe,
    )

    print("\n--- Résultat du test ---")
    if backtest_results and not backtest_results.get("error"):
        print(
            "✅ Le BacktestEngine s'est initialisé et a tenté de charger les données avec succès."
        )
        print(f"Résultats bruts (actuellement des placeholders): {backtest_results}")
    else:
        print(
            "❌ Le BacktestEngine a rencontré un problème lors de l'initialisation ou du chargement des données."
        )
        print(f"Détails de l'erreur: {backtest_results.get('error', 'Non spécifié')}")

    print("\n--- Fin du Test du BacktestEngine ---")