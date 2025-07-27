# data_models/phase_observer_models.py

from pydantic import BaseModel, Field, validator
from typing import Dict, List, Optional, Any, Union
from datetime import datetime

# --- Modèles pour les détails de signaux complexes (dictionnaires imbriqués) ---

class FVGDetails(BaseModel):
    """Modèle Pydantic pour les détails d'un Fair Value Gap (FVG)."""
    type: str = Field(..., description="Type de FVG (ex: 'bullish', 'bearish').")
    top: float = Field(..., description="Niveau de prix supérieur du FVG.")
    bottom: float = Field(..., description="Niveau de prix inférieur du FVG.")
    # Vous pouvez ajouter ici d'autres champs si votre detect_fvg les génère (ex: 'magnitude')

class OBDetails(BaseModel):
    """Modèle Pydantic pour les détails d'un Order Block (OB)."""
    type: str = Field(..., description="Type d'OB (ex: 'bullish', 'bearish').")
    zone: List[float] = Field(..., min_items=2, max_items=2, description="Zone de prix de l'OB [low, high].")
    criteria_details: Dict[str, bool] = Field(..., description="Détails des critères de confluence de l'OB (ex: 'fvg_confluence': True).")
    # Note: Le 'score' de l'OB a été supprimé du PhaseObserver.

class BOSMSSDetails(BaseModel):
    """Modèle Pydantic pour les détails d'un Break of Structure (BOS) ou Market Structure Shift (MSS)."""
    type: str = Field(..., description="Type de cassure (ex: 'bullish_bos', 'bearish_mss').")
    level_broken: float = Field(..., description="Niveau de prix qui a été cassé.")
    # Vous pouvez ajouter ici d'autres champs si votre detect_bos_mss les génère

class LiquidityGrabDetails(BaseModel):
    """Modèle Pydantic pour les détails d'une Prise de Liquidité (Liquidity Grab / Sweep)."""
    type: str = Field(..., description="Type de prise de liquidité (ex: 'bullish_sweep', 'bearish_sweep').")
    level_swept: float = Field(..., description="Niveau de prix où la liquidité a été balayée.")

class VolumeAnomalyDetails(BaseModel):
    """Modèle Pydantic pour les détails d'une anomalie de volume."""
    type: str = Field(..., description="Type d'anomalie de volume (ex: 'spike', 'drought', 'normal').")
    z_score: float = Field(..., description="Z-score du volume actuel par rapport à la moyenne roulante.")
    volume_momentum: float = Field(..., description="Momentum du volume (-1.0 à 1.0).")

class EQHEQLDetails(BaseModel):
    """Modèle Pydantic pour les détails des Equal Highs (EQH) ou Equal Lows (EQL)."""
    type: str = Field(..., description="Type de niveau d'égalité (ex: 'EQH', 'EQL').")
    level: float = Field(..., description="Niveau de prix de l'égalité.")
    confluence_count: int = Field(..., description="Nombre de points de swing formant la confluence.")
    indices_involved: List[str] = Field(..., description="Horodatages des bougies impliquées (ISO format).")

class NearestLiquidityLevelDetails(BaseModel):
    """Modèle Pydantic pour les détails du niveau de liquidité le plus proche."""
    type: str = Field(..., description="Type de niveau (ex: 'EQH', 'OB_low', 'FVG_top').")
    level: float = Field(..., description="Niveau de prix de la liquidité.")
    distance_pips: float = Field(..., ge=0, description="Distance en pips du prix actuel à ce niveau.")


# --- Modèle Pydantic pour une seule ligne du DataFrame annoté du PhaseObserver ---

class PhaseObserverRowModel(BaseModel):
    """
    Modèle Pydantic pour valider la structure et les types de données d'une ligne
    du DataFrame annoté retourné par PhaseObserver.analyze().
    """
    # Données brutes OHLCV (de base)
    open: float = Field(..., description="Prix d'ouverture de la bougie.")
    high: float = Field(..., description="Prix le plus haut de la bougie.")
    low: float = Field(..., description="Prix le plus bas de la bougie.")
    close: float = Field(..., description="Prix de clôture de la bougie.")
    tick_volume: float = Field(..., ge=0, description="Volume de ticks de la bougie (>=0).")

    # Informations MT5 ajoutées au DataFrame par mt5_connector
    spread: float = Field(..., description="Spread de l'actif en points.")
    point: float = Field(..., gt=0, description="Valeur d'un point pour l'actif (doit être > 0).")
    trade_tick_size: float = Field(..., ge=0, description="Taille minimale du tick pour le trading (>=0).")
    trade_contract_size: float = Field(..., gt=0, description="Taille du contrat de l'actif (doit être > 0).")

    # Colonnes de contexte de marché et de phase
    phase: str = Field(..., description="Phase de marché détectée (ex: 'trending_up', 'consolidation').")
    trend: str = Field(..., description="Tendance dominante ('bullish', 'bearish', 'neutral').")
    
    # Indicateurs de détection (booléens)
    fvg_detected: bool = Field(..., description="Indique si un FVG a été détecté.")
    ob_detected: bool = Field(..., description="Indique si un Order Block a été détecté.")
    bos_mss_detected: bool = Field(..., description="Indique si un BOS/MSS a été détecté.")
    liquidity_grab_detected: bool = Field(..., description="Indique si une prise de liquidité a été détectée.")
    volume_anomaly_detected: bool = Field(..., description="Indique si une anomalie de volume a été détectée.")
    eqh_eql_detected: bool = Field(..., description="Indique si des EQH/EQL ont été détectés.")

    # Signaux de confirmation "chirurgicaux" (booléens)
    entry_confirmation_bullish: bool = Field(..., description="Confirmation d'entrée haussière (ex: tap FVG + bougie de rejet).")
    entry_confirmation_bearish: bool = Field(..., description="Confirmation d'entrée baissière (ex: tap FVG + bougie de rejet).")
    
    # Validation de setups (booléens)
    validated_ob: bool = Field(..., description="Indique si l'Order Block est validé par critères de confluence.")
    validated_ob_fvg: bool = Field(..., description="Indique si l'OB est validé par FVG.")
    validated_ob_bos: bool = Field(..., description="Indique si l'OB est validé par BOS/MSS.")
    validated_ob_trend: bool = Field(..., description="Indique si l'OB est validé par alignement de tendance.")
    validated_ob_mitigated: bool = Field(..., description="Indique si l'OB est validé par non-mitigation (reste frais).")

    # Détails des signaux (modèles Pydantic imbriqués, Optional car peuvent être None)
    fvg_details: Optional[FVGDetails] = Field(None, description="Détails du FVG détecté.")
    ob_details: Optional[OBDetails] = Field(None, description="Détails de l'Order Block détecté.")
    bos_mss_details: Optional[BOSMSSDetails] = Field(None, description="Détails du BOS/MSS détecté.")
    liquidity_grab_details: Optional[LiquidityGrabDetails] = Field(None, description="Détails de la prise de liquidité.")
    volume_anomaly_details: Optional[VolumeAnomalyDetails] = Field(None, description="Détails de l'anomalie de volume.")
    eqh_eql_details: Optional[EQHEQLDetails] = Field(None, description="Détails des EQH/EQL détectés.")
    
    # Autres métriques ou détails du contexte
    volume_momentum: float = Field(..., description="Momentum du volume (peut être positif ou négatif).")
    nearest_liquidity_level_details: Optional[NearestLiquidityLevelDetails] = Field(None, description="Détails du niveau de liquidité le plus proche.")
    is_liquid: bool = Field(..., description="Indique si l'actif est actuellement considéré comme liquide.")

    # Index du DataFrame (timestamp)
    timestamp: str = Field(..., description="Horodatage de la ligne (bougie) au format ISO 8601 UTC.")


    # --- Validateurs personnalisés (si nécessaire pour des contraintes plus fines) ---
    @validator('open', 'high', 'low', 'close', 'point', 'trade_tick_size', 'trade_contract_size')
    def check_positive_values(cls, v):
        if v is not None and v < 0: # `v is not None` car certains peuvent être 0 (trade_tick_size)
            raise ValueError('Values must be non-negative.')
        if v is not None and v == 0 and cls.__name__ in ['OBDetails', 'FVGDetails'] and cls.__fields__[v].field_info.description in ["Niveau de prix supérieur", "Niveau de prix inférieur", "Prix d'ouverture", "Prix de clôture", "Prix le plus haut", "Prix le plus bas"]: # Prix ne peut être 0
             raise ValueError('Price cannot be zero.')
        return v
    
    @validator('spread')
    def check_positive_spread(cls, v):
        if v < 0:
            raise ValueError('Spread must be non-negative.')
        return v

    @validator('trade_tick_size', 'trade_contract_size', 'point')
    def check_non_zero_for_division(cls, v):
        if v is not None and v == 0:
            raise ValueError('This value cannot be zero as it is used in division.')
        return v

    @validator('timestamp')
    def validate_timestamp_format(cls, v):
        try:
            datetime.fromisoformat(v.replace('Z', '+00:00')) # Gérer le 'Z' de l'ISO format
        except ValueError:
            raise ValueError("Timestamp doit être au format ISO 8601.")
        return v