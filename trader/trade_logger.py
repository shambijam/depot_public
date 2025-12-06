# trader/trade_logger.py
"""
Système de journalisation des trades pour analyse de performance.

Ce module capture TOUTES les métriques à l'entrée et la sortie de chaque trade
pour permettre l'optimisation data-driven du système de scoring.

Session: 25 Novembre 2025
Objectif: Améliorer le scoring en se basant sur des données réelles
"""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime, UTC
from typing import Any, Dict, Optional
from pathlib import Path


class TradeLogger:
    """
    Journalisation complète des trades pour analyse de performance.

    Chaque trade est enregistré avec:
    - Métriques d'entrée (scores, triggers, qualité données)
    - Résultat de sortie (WIN/LOSS, PnL, durée, raison)

    Format de sortie: JSON Lines (1 trade par ligne)
    Fichier: logs/trades_history.jsonl
    """

    def __init__(self, config_manager):
        self.config_manager = config_manager
        self.logger = logging.getLogger(__name__)

        # Chemins des fichiers de journalisation
        logs_dir = Path("logs")
        logs_dir.mkdir(exist_ok=True)
        self.log_file_jsonl = logs_dir / "trades_history.jsonl"  # Pour analyse automatique
        self.log_file_md = logs_dir / "trades_history.md"        # Pour lecture humaine

        # Cache des trades en cours (basket_id -> trade_data)
        self._active_trades: Dict[str, Dict[str, Any]] = {}

        # Créer header MD si fichier n'existe pas
        if not self.log_file_md.exists():
            self._write_md_header()

        self.logger.info(f"✅ TradeLogger initialisé: {self.log_file_jsonl} + {self.log_file_md}")

    def log_trade_entry(
        self,
        basket_id: str,
        symbol: str,
        direction: str,
        entry_price: float,
        volume: float,
        sl_price: float,
        tp_price: float,
        # Métriques de scoring
        score_final: float,
        score_base: float,
        of_score: float,
        fp_score: float,
        trigger_type: str,
        trigger_confidence: float,
        trigger_boost: float,
        quality_multiplier: float,
        # Qualité données
        tick_count: int,
        coverage_s: float,
        status_of: str,
        status_fp: str,
        # Cohérence
        aligned_3_of_3: bool,
        conflicts_count: int,
        # Contexte
        strategy: str = "scalping",
        burst_size: int = 1,
        # ✅ MAJ (06 DEC 2025): Phase de marché pour analyse performance
        market_phase: Optional[str] = None,
        market_regime: Optional[str] = None,
        vwap_regime: Optional[str] = None,
        phase_confidence: Optional[float] = None,
        vwap_score: Optional[float] = None,
        **extra_params
    ) -> None:
        """
        Enregistre l'ENTRÉE d'un trade avec toutes ses métriques.

        Args:
            basket_id: Identifiant unique du trade/basket
            symbol: Asset tradé (XAUUSD, EURUSD, etc.)
            direction: BUY ou SELL
            entry_price: Prix d'entrée moyen
            volume: Volume total (somme des positions si burst)
            sl_price: Prix du stop loss
            tp_price: Prix du take profit
            score_final: Score fusionné final (0.0-0.99)
            score_base: Score de base (OF+FP)/2
            of_score: Score OrderFlow V6
            fp_score: Score Footprint M1
            trigger_type: Type de trigger (stacking, climax, etc.) ou "none"
            trigger_confidence: Confiance du trigger (0.0-0.99)
            trigger_boost: Bonus appliqué par le trigger
            quality_multiplier: Multiplicateur qualité données (0.0-1.0)
            tick_count: Nombre de ticks analysés
            coverage_s: Durée couverte en secondes
            status_of: Status OrderFlow (VALID/SUSPECT)
            status_fp: Status Footprint (VALID/SUSPECT)
            aligned_3_of_3: Consensus unanime des 3 fonctions
            conflicts_count: Nombre de conflits détectés
            strategy: Nom de la stratégie (scalping, liquidity, etc.)
            burst_size: Nombre de positions du burst
            **extra_params: Paramètres supplémentaires
        """

        trade_data = {
            # === IDENTITÉ ===
            "trade_id": basket_id,
            "symbol": symbol,
            "strategy": strategy,
            "direction": direction,

            # === TIMESTAMPS ===
            "entry_time": datetime.now(UTC).isoformat(),
            "entry_timestamp": datetime.now(UTC).timestamp(),

            # === ENTRÉE ===
            "entry_price": float(entry_price),
            "volume": float(volume),
            "burst_size": int(burst_size),
            "sl_price": float(sl_price),
            "tp_price": float(tp_price),
            "sl_distance_pips": abs(entry_price - sl_price) * 100,  # XAUUSD: 1 pip = 0.01
            "tp_distance_pips": abs(tp_price - entry_price) * 100,
            "risk_reward_ratio": abs(tp_price - entry_price) / abs(entry_price - sl_price) if abs(entry_price - sl_price) > 0 else 0.0,

            # === SCORING ===
            "score_final": float(score_final),
            "score_base": float(score_base),
            "score_of": float(of_score),
            "score_fp": float(fp_score),

            # === TRIGGER ===
            "trigger_type": str(trigger_type),
            "trigger_confidence": float(trigger_confidence),
            "trigger_boost": float(trigger_boost),
            "has_real_trigger": trigger_type not in ["none", "fusion_pretrigger", ""],

            # === QUALITÉ DONNÉES ===
            "tick_count": int(tick_count),
            "coverage_s": float(coverage_s),
            "tick_rate": float(tick_count / coverage_s) if coverage_s > 0 else 0.0,
            "status_of": str(status_of),
            "status_fp": str(status_fp),
            "quality_multiplier": float(quality_multiplier),

            # === COHÉRENCE ===
            "aligned_3_of_3": bool(aligned_3_of_3),
            "conflicts_count": int(conflicts_count),

            # === CATÉGORIE SCORING ===
            "score_category": self._categorize_score(score_final),

            # === PHASE DE MARCHÉ (06 DEC 2025) ===
            "market_phase": str(market_phase) if market_phase else None,  # Phase optimisée (liquidity_sweep, trending_institutional_bull, etc.)
            "market_regime": str(market_regime) if market_regime else None,  # Régime PhaseObserver (trending_institutional_bull, range_accumulation, etc.)
            "vwap_regime": str(vwap_regime) if vwap_regime else None,  # Régime VWAP (TRENDING, ACCUMULATION, BALANCED, TRANSITIONAL)
            "phase_confidence": float(phase_confidence) if phase_confidence is not None else None,
            "vwap_score": float(vwap_score) if vwap_score is not None else None,

            # === RÉSULTAT (à remplir à la sortie) ===
            "outcome": None,  # WIN / LOSS / BE (Break-Even)
            "exit_time": None,
            "exit_price": None,
            "pnl_pips": None,
            "pnl_usd": None,
            "duration_minutes": None,
            "exit_reason": None,  # trailing_stop, tp_hit, sl_hit, manual, timeout
            "max_favorable_excursion_pips": None,  # MFE: meilleur profit atteint
            "max_adverse_excursion_pips": None,    # MAE: pire drawdown atteint

            # === EXTRA ===
            **extra_params
        }

        # Stocker dans le cache des trades actifs
        self._active_trades[basket_id] = trade_data

        # Écrire l'entrée immédiatement dans le fichier
        self._write_to_file(trade_data)

        self.logger.info(
            f"📝 [TRADE_LOG][ENTRY] {basket_id} | {symbol} {direction} @ {entry_price:.2f} | "
            f"Score: {score_final:.1%} ({self._categorize_score(score_final)}) | "
            f"Trigger: {trigger_type} ({trigger_confidence:.1%})"
        )

    def log_trade_exit(
        self,
        basket_id: str,
        exit_price: float,
        pnl_pips: float,
        pnl_usd: float,
        exit_reason: str,
        outcome: Optional[str] = None,
        max_favorable_excursion_pips: Optional[float] = None,
        max_adverse_excursion_pips: Optional[float] = None,
        **extra_params
    ) -> None:
        """
        Enregistre la SORTIE d'un trade et finalise l'entrée dans le log.

        Args:
            basket_id: Identifiant du trade
            exit_price: Prix de sortie moyen
            pnl_pips: Profit/Perte en pips
            pnl_usd: Profit/Perte en USD
            exit_reason: Raison de sortie (trailing_stop, tp_hit, sl_hit, manual, timeout)
            outcome: WIN/LOSS/BE (calculé auto si None)
            max_favorable_excursion_pips: Meilleur profit atteint pendant le trade
            max_adverse_excursion_pips: Pire drawdown atteint pendant le trade
            **extra_params: Paramètres supplémentaires
        """

        if basket_id not in self._active_trades:
            self.logger.warning(f"⚠️ [TRADE_LOG][EXIT] Trade {basket_id} non trouvé dans cache actif (skip)")
            return

        trade_data = self._active_trades[basket_id]

        # Compléter les données de sortie
        exit_time = datetime.now(UTC)
        entry_time = datetime.fromisoformat(trade_data["entry_time"])
        duration_minutes = (exit_time - entry_time).total_seconds() / 60.0

        # Déterminer outcome si non fourni
        if outcome is None:
            if abs(pnl_pips) < 2.0:  # Break-Even (< 2 pips)
                outcome = "BE"
            elif pnl_pips > 0:
                outcome = "WIN"
            else:
                outcome = "LOSS"

        trade_data.update({
            "exit_time": exit_time.isoformat(),
            "exit_timestamp": exit_time.timestamp(),
            "exit_price": float(exit_price),
            "pnl_pips": float(pnl_pips),
            "pnl_usd": float(pnl_usd),
            "duration_minutes": float(duration_minutes),
            "exit_reason": str(exit_reason),
            "outcome": str(outcome),
            "max_favorable_excursion_pips": float(max_favorable_excursion_pips) if max_favorable_excursion_pips is not None else None,
            "max_adverse_excursion_pips": float(max_adverse_excursion_pips) if max_adverse_excursion_pips is not None else None,
            **extra_params
        })

        # Écrire dans le fichier de log (JSON Lines)
        self._write_to_file(trade_data)

        # Retirer du cache actif
        del self._active_trades[basket_id]

        self.logger.info(
            f"📝 [TRADE_LOG][EXIT] {basket_id} | {outcome} | "
            f"PnL: {pnl_pips:+.1f} pips ({pnl_usd:+.2f} USD) | "
            f"Duration: {duration_minutes:.1f}min | Reason: {exit_reason}"
        )

    def _categorize_score(self, score: float) -> str:
        """Catégorise le score final selon les seuils de décision."""
        if score >= 0.90:
            return "DIAMANT"
        elif score >= 0.80:
            return "PLATINE"
        elif score >= 0.70:
            return "OR"
        elif score >= 0.55:
            return "ARGENT"
        else:
            return "BRONZE"

    def _write_md_header(self) -> None:
        """Crée le header du fichier Markdown."""
        try:
            with open(self.log_file_md, 'w', encoding='utf-8') as f:
                f.write("# 📊 Historique des Trades\n\n")
                f.write("*Journal généré automatiquement par TradeLogger*\n\n")
                f.write("---\n\n")
        except Exception as e:
            self.logger.error(f"❌ [TRADE_LOG] Erreur création header MD: {e}", exc_info=True)

    def _write_to_file(self, trade_data: Dict[str, Any]) -> None:
        """Écrit une entrée de trade dans les fichiers JSON Lines et Markdown."""
        basket_id = trade_data.get("trade_id", "UNKNOWN")

        # 1. Format JSON Lines (pour analyse automatique)
        try:
            with open(self.log_file_jsonl, 'a', encoding='utf-8') as f:
                json.dump(trade_data, f, ensure_ascii=False)
                f.write('\n')
            self.logger.info(f"✅ [TRADE_LOG][DEBUG] JSONL écrit pour {basket_id}")
        except Exception as e:
            self.logger.error(f"❌ [TRADE_LOG] Erreur écriture JSONL: {e}", exc_info=True)

        # 2. Format Markdown (pour lecture humaine)
        try:
            self._write_md_entry(trade_data)
            self.logger.info(f"✅ [TRADE_LOG][DEBUG] MD écrit pour {basket_id}")
        except Exception as e:
            self.logger.error(f"❌ [TRADE_LOG] Erreur écriture MD: {e}", exc_info=True)

    def _write_md_entry(self, td: Dict[str, Any]) -> None:
        """Écrit une entrée de trade en format Markdown lisible."""
        with open(self.log_file_md, 'a', encoding='utf-8') as f:
            # Header du trade avec résultat
            outcome = td.get("outcome", "PENDING")
            emoji = "🟢" if outcome == "WIN" else "🔴" if outcome == "LOSS" else "🟡" if outcome == "BE" else "⏳"

            # Direction avec emoji
            direction = td.get('direction', 'UNKNOWN')
            dir_emoji = "🟢" if direction == "BUY" else "🔴" if direction == "SELL" else "⚪"

            # Score catégorie avec emoji
            score_cat = td.get('score_category', 'UNKNOWN')
            cat_emoji = {"DIAMANT": "💎", "PLATINE": "🔷", "OR": "🟡", "ARGENT": "🔘", "BRONZE": "⚪"}.get(score_cat, "❓")

            # Trigger avec emoji
            has_trigger = td.get('has_real_trigger', False)
            trigger_type = td.get('trigger_type', 'none')
            trigger_emoji = "🎯" if has_trigger else "❌"

            # Extraire l'heure depuis l'ISO timestamp
            entry_time_str = td.get('entry_time', '')
            if entry_time_str:
                try:
                    entry_dt = datetime.fromisoformat(entry_time_str)
                    time_display = entry_dt.strftime("%H:%M:%S")
                except:
                    time_display = entry_time_str
            else:
                time_display = "N/A"

            # Header enrichi avec toutes les infos principales
            f.write(f"## {emoji} Trade #{td.get('trade_id', 'UNKNOWN')}\n\n")
            f.write(f"**⏰ {time_display}** | "
                   f"{dir_emoji} **{direction}** {td.get('symbol', 'N/A')} @ **{td.get('entry_price', 0):.2f}** | "
                   f"{cat_emoji} **{score_cat}** ({td.get('score_final', 0):.1%}) | "
                   f"{trigger_emoji} **{trigger_type}** ({td.get('trigger_confidence', 0):.1%})\n\n")

            f.write(f"📊 **Scores:** OF={td.get('score_of', 0):.1%} | FP={td.get('score_fp', 0):.1%} | "
                   f"Base={td.get('score_base', 0):.1%} | Boost={td.get('trigger_boost', 0):+.1%}\n\n")

            # Informations de base
            f.write("### 📋 Informations\n\n")
            f.write(f"| Champ | Valeur |\n")
            f.write(f"|-------|--------|\n")
            f.write(f"| **Symbol** | {td.get('symbol')} |\n")
            f.write(f"| **Direction** | {td.get('direction')} |\n")
            f.write(f"| **Stratégie** | {td.get('strategy')} |\n")
            f.write(f"| **Burst Size** | {td.get('burst_size')} positions |\n")
            f.write(f"| **Entry Time** | {td.get('entry_time')} |\n")

            if td.get('exit_time'):
                f.write(f"| **Exit Time** | {td.get('exit_time')} |\n")
                f.write(f"| **Duration** | {td.get('duration_minutes', 0):.1f} min |\n")

            f.write("\n")

            # Prix et volumes
            f.write("### 💰 Position\n\n")
            f.write(f"| Champ | Valeur |\n")
            f.write(f"|-------|--------|\n")
            f.write(f"| **Entry Price** | {td.get('entry_price', 0):.2f} |\n")
            f.write(f"| **Volume Total** | {td.get('volume', 0):.2f} lots |\n")
            f.write(f"| **Stop Loss** | {td.get('sl_price', 0):.2f} ({td.get('sl_distance_pips', 0):.0f} pips) |\n")
            f.write(f"| **Take Profit** | {td.get('tp_price', 0):.2f} ({td.get('tp_distance_pips', 0):.0f} pips) |\n")
            f.write(f"| **Risk/Reward** | {td.get('risk_reward_ratio', 0):.2f} |\n")

            if td.get('exit_price'):
                f.write(f"| **Exit Price** | {td.get('exit_price', 0):.2f} |\n")

            f.write("\n")

            # Qualité données
            f.write(f"### 📊 Qualité Données\n\n")
            f.write(f"| Métrique | Valeur |\n")
            f.write(f"|----------|--------|\n")
            f.write(f"| **Tick Count** | {td.get('tick_count', 0)} ticks |\n")
            f.write(f"| **Coverage** | {td.get('coverage_s', 0):.1f}s |\n")
            f.write(f"| **Tick Rate** | {td.get('tick_rate', 0):.2f} ticks/s |\n")
            f.write(f"| **Status OF** | {td.get('status_of', 'UNKNOWN')} |\n")
            f.write(f"| **Status FP** | {td.get('status_fp', 'UNKNOWN')} |\n")
            f.write("\n")

            # Cohérence
            aligned = "✅ OUI" if td.get('aligned_3_of_3') else "❌ NON"
            conflicts = td.get('conflicts_count', 0)

            f.write(f"### 🔗 Cohérence\n\n")
            f.write(f"| Métrique | Valeur |\n")
            f.write(f"|----------|--------|\n")
            f.write(f"| **Alignement 3/3** | {aligned} |\n")
            f.write(f"| **Conflits** | {conflicts} |\n")
            f.write("\n")

            # ✅ Phase de marché (06 DEC 2025)
            if td.get('market_phase') or td.get('market_regime') or td.get('vwap_regime'):
                f.write(f"### 📈 Phase de Marché\n\n")
                f.write(f"| Métrique | Valeur |\n")
                f.write(f"|----------|--------|\n")

                if td.get('market_phase'):
                    f.write(f"| **Phase Optimisée** | {td.get('market_phase')} |\n")
                if td.get('market_regime'):
                    f.write(f"| **Régime PhaseObserver** | {td.get('market_regime')} |\n")
                if td.get('vwap_regime'):
                    vwap_regime_emoji = {"TRENDING": "📈", "ACCUMULATION": "📊", "BALANCED": "⚖️", "TRANSITIONAL": "🔄"}.get(td.get('vwap_regime'), "❓")
                    f.write(f"| **Régime VWAP** | {vwap_regime_emoji} {td.get('vwap_regime')} |\n")
                if td.get('phase_confidence') is not None:
                    f.write(f"| **Confiance Phase** | {td.get('phase_confidence'):.1%} |\n")
                if td.get('vwap_score') is not None:
                    f.write(f"| **Score VWAP** | {td.get('vwap_score'):.1%} |\n")

                f.write("\n")

            # Résultat
            if td.get('outcome'):
                pnl_pips = td.get('pnl_pips', 0)
                pnl_usd = td.get('pnl_usd', 0)
                exit_reason = td.get('exit_reason', 'unknown')

                result_emoji = "🟢" if pnl_pips > 0 else "🔴" if pnl_pips < 0 else "🟡"

                f.write(f"### {result_emoji} Résultat - {outcome}\n\n")
                f.write(f"| Métrique | Valeur |\n")
                f.write(f"|----------|--------|\n")
                f.write(f"| **PnL Pips** | {pnl_pips:+.1f} pips |\n")
                f.write(f"| **PnL USD** | {pnl_usd:+.2f} USD |\n")
                f.write(f"| **Exit Reason** | {exit_reason} |\n")

                if td.get('max_favorable_excursion_pips') is not None:
                    f.write(f"| **MFE** | {td.get('max_favorable_excursion_pips', 0):+.1f} pips |\n")
                if td.get('max_adverse_excursion_pips') is not None:
                    f.write(f"| **MAE** | {td.get('max_adverse_excursion_pips', 0):+.1f} pips |\n")

                f.write("\n")

            # Séparateur
            f.write("---\n\n")

    def get_active_trades_count(self) -> int:
        """Retourne le nombre de trades actuellement actifs."""
        return len(self._active_trades)

    def get_active_trade(self, basket_id: str) -> Optional[Dict[str, Any]]:
        """Récupère les données d'un trade actif."""
        return self._active_trades.get(basket_id)

    def cleanup_stale_trades(self, max_age_hours: int = 24) -> int:
        """
        Nettoie les trades actifs qui sont trop vieux (probablement fermés sans notification).

        Args:
            max_age_hours: Âge maximum en heures

        Returns:
            Nombre de trades nettoyés
        """
        now = datetime.now(UTC).timestamp()
        max_age_seconds = max_age_hours * 3600

        stale_trades = []
        for basket_id, trade_data in self._active_trades.items():
            entry_timestamp = trade_data.get("entry_timestamp", now)
            age_seconds = now - entry_timestamp

            if age_seconds > max_age_seconds:
                stale_trades.append(basket_id)

        # Marquer comme TIMEOUT et écrire
        for basket_id in stale_trades:
            trade_data = self._active_trades[basket_id]
            trade_data.update({
                "exit_time": datetime.now(UTC).isoformat(),
                "exit_reason": "timeout_cleanup",
                "outcome": "UNKNOWN",
                "duration_minutes": (now - trade_data.get("entry_timestamp", now)) / 60.0
            })
            self._write_to_file(trade_data)
            del self._active_trades[basket_id]
            self.logger.warning(f"⚠️ [TRADE_LOG] Trade {basket_id} nettoyé (timeout {max_age_hours}h)")

        return len(stale_trades)
