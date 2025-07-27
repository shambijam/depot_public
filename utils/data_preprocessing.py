import pandas as pd
from datetime import datetime, UTC
import logging
from pathlib import Path
from typing import Optional

# Configurer le logger pour ce module
#logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

def load_and_preprocess_dukascopy_csv(
    file_path: Path,
    symbol: str, # Nouveau: le symbole de l'actif
    timeframe: str, # Nouveau: le timeframe
    estimated_spread_pips: float = 0.5, # Estimation du spread en pips
    fixed_point_value: Optional[float] = None # Nouveau: Valeur du point si connue (ex: 0.00001 pour la plupart des paires FX)
) -> pd.DataFrame:
    """
    Charge et prépare un fichier CSV de données historiques de Dukascopy,
    en assurant la standardisation et la gestion des données pour le backtesting.

    Args:
        file_path (Path): Le chemin vers le fichier CSV de Dukascopy.
        symbol (str): Le symbole de l'actif (ex: "EURUSD", "BTCUSD"). Utilisé pour les logs et la gestion du point.
        timeframe (str): Le timeframe des données (ex: "M1"). Utilisé pour les logs.
        estimated_spread_pips (float): Une estimation du spread en pips à ajouter si la colonne spread n'est pas présente.
                                        Crucial pour simuler les entrées BUY.
        fixed_point_value (Optional[float]): Valeur du "point" de l'actif (ex: 0.00001 pour EURUSD).
                                                Si None, tentera de déduire ou utilisera une valeur par défaut.

    Returns:
        pd.DataFrame: Le DataFrame nettoyé et standardisé avec les colonnes attendues
                      (time, open, high, low, close, tick_volume, spread, point, trade_tick_size, trade_contract_size).
                      Retourne un DataFrame vide en cas d'erreur ou si aucune donnée.
    """
    logger.info(f"Début du prétraitement pour {symbol} ({timeframe}) depuis {file_path.name}")

    try:
        # 1. Lecture du fichier CSV - Détection automatique du séparateur
        try:
            df = pd.read_csv(file_path, sep=',')
        except pd.errors.ParserError:
            logger.warning(f"Virgule non trouvée comme séparateur pour {file_path.name}. Essai avec point-virgule.")
            df = pd.read_csv(file_path, sep=';')
        
        if df.empty:
            logger.warning(f"Le fichier {file_path.name} est vide après lecture.")
            return df

        # 2. Standardisation des noms de colonnes
        # CORRECTION ICI : Renommer 'Volume' en 'tick_volume' pour correspondre aux attentes du PhaseObserver.
        df.rename(columns={
            'Local time': 'time',
            'Gmt time': 'time',   
            'Open': 'open',
            'High': 'high',
            'Low': 'low',
            'Close': 'close',
            'Volume': 'tick_volume' # CORRECTION : Renommé en 'tick_volume'
        }, inplace=True)

        # Vérifier si la colonne 'time' a bien été renommée (elle doit exister maintenant)
        if 'time' not in df.columns:
            raise ValueError("La colonne de temps ('Local time' ou 'Gmt time') n'a pas été trouvée ou renommée correctement.")

        # 3. Conversion de la colonne 'time' en datetime (UTC) et définition comme index
        df['time'] = pd.to_datetime(df['time'], utc=True, errors='coerce')
        df.dropna(subset=['time'], inplace=True) # Supprimer les lignes avec des temps invalides

        if df.empty:
            logger.warning(f"Plus aucune donnée après suppression des lignes avec temps invalides dans {file_path.name}.")
            return df

        df.set_index('time', inplace=True)
        df.sort_index(inplace=True) # S'assurer que le DataFrame est trié chronologiquement

        # Gérer les doublons d'index (horodatage) - Garder la première occurrence
        initial_len_dedup = len(df)
        df = df.loc[~df.index.duplicated(keep="first")]
        if len(df) < initial_len_dedup:
            logger.warning(f"Supprimé {initial_len_dedup - len(df)} lignes dupliquées basées sur l'horodatage pour {file_path.name}.")

        # 4. Conversion des colonnes OHLCV et tick_volume en float et gestion des valeurs non-positives/manquantes
        # CORRECTION ICI : Utiliser 'tick_volume' au lieu de 'volume'
        numeric_cols = ['open', 'high', 'low', 'close', 'tick_volume']
        for col in numeric_cols:
            if col in df.columns:
                df[col] = pd.to_numeric(df[col], errors='coerce').fillna(0.0)
                # Correction des valeurs non-positives
                if col in ['open', 'high', 'low', 'close'] and (df[col] <= 0).any():
                    count_fixed = df.loc[df[col] <= 0].shape[0]
                    df.loc[df[col] <= 0, col] = 0.00001 # Prix minimal pour éviter division par zéro
                    logger.warning(f"{count_fixed} valeurs non-positives corrigées dans '{col}' pour {file_path.name}.")
                elif col == 'tick_volume' and (df[col] < 0).any(): # CORRECTION : 'tick_volume'
                    count_fixed = df.loc[df[col] < 0].shape[0]
                    df.loc[df[col] < 0, col] = 0.0 # Le volume ne peut pas être négatif
                    logger.warning(f"{count_fixed} volumes négatifs corrigés dans '{col}' pour {file_path.name}.")
            else:
                logger.warning(f"La colonne essentielle '{col}' est manquante pour {file_path.name}. Elle sera remplie avec des zéros.")
                df[col] = 0.0

        # 5. Déterminer la valeur du point (pour le calcul du spread)
        # La valeur du point sera celle fournie ou estimée.
        if fixed_point_value is not None:
            point_value = fixed_point_value
        elif 'JPY' in symbol.upper(): # Estimation simple pour JPY
            point_value = 0.001
        elif 'USD' in symbol.upper() and ('BTC' in symbol.upper() or 'ETH' in symbol.upper() or 'SOL' in symbol.upper() or 'LTC' in symbol.upper()): # Cryptos
            point_value = 0.01 
            logger.warning(f"Point_value estimé pour Crypto '{symbol}'. Confirmez si {point_value} est approprié pour votre usage du 'pip'.")
        else: # Par défaut pour la plupart des paires FX
            point_value = 0.00001
        
        if point_value <= 0: 
            logger.error(f"Valeur de point invalide ({point_value}) pour {symbol}. Utilisation de 0.00001.")
            point_value = 0.00001

        # 6. Ajout de la colonne 'spread'
        if 'spread' not in df.columns:
            df['spread'] = estimated_spread_pips * point_value
            logger.info(f"Colonne 'spread' ajoutée avec une estimation fixe de {estimated_spread_pips} pips ({df['spread'].iloc[0]:.5f} en valeur absolue).")
        else:
            df['spread'] = pd.to_numeric(df['spread'], errors='coerce').fillna(0.0)
            logger.info(f"Colonne 'spread' existante traitée. Valeurs min/max : {df['spread'].min():.5f} / {df['spread'].max():.5f}")

        # --- DÉBUT DES NOUVELLES COLONNES POUR PHASEOBSERVER ET BACKTESTENGINE ---
        # 7. Ajout des colonnes 'point', 'trade_tick_size', 'trade_contract_size' au DataFrame
        # Ces colonnes sont nécessaires pour PhaseObserver et BacktestEngine pour les calculs.
        # Leurs valeurs sont passées à load_and_preprocess_dukascopy_csv.
        
        # 'point' est déjà calculé/déterminé ci-dessus
        df['point'] = point_value

        # 'trade_tick_size' est souvent 1 point (la plus petite variation de prix)
        # Nous pouvons l'initialiser ici par défaut à la valeur du point.
        df['trade_tick_size'] = point_value 
        logger.info(f"Colonne 'trade_tick_size' ajoutée/mise à jour à {point_value}.")
        
        # 'trade_contract_size' doit venir de la config de l'actif ou d'un défaut.
        # Pour les tests, on peut utiliser des défauts basés sur le type.
        if 'BTC' in symbol.upper() or 'ETH' in symbol.upper() or 'SOL' in symbol.upper() or 'LTC' in symbol.upper():
            contract_size = 1.0 # Pour les cryptos, 1 lot = 1 unité
        else:
            contract_size = 100000.0 # Pour le Forex/XAU, 1 lot = 100,000 unités de base
        
        df['trade_contract_size'] = contract_size
        logger.info(f"Colonne 'trade_contract_size' ajoutée/mise à jour à {contract_size}.")

        # --- FIN DES NOUVELLES COLONNES ---


        logger.info(f"Prétraitement du fichier {file_path.name} terminé avec succès. {len(df)} barres traitées.")
        return df

    except Exception as e:
        logger.critical(f"ERREUR CRITIQUE lors du prétraitement du fichier {file_path.name}: {e}", exc_info=True)
        return pd.DataFrame()