#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
orchestration/workers/dashboard_worker.py - Thread d'affichage du dashboard

Affiche un tableau consolidé de tous les assets scalping toutes les 5 secondes.
"""

import logging
import queue
import time
import threading

import pandas as pd


def dashboard_worker(
    display_queue: queue.Queue,
    stop_event: threading.Event,
    logger,
    verbose: bool = False
):
    """
    Thread dashboard: vide la queue et affiche tableau consolidé toutes les 5 secondes.

    (05 JAN 2026): Draine TOUTE la queue à chaque cycle pour éviter accumulation.
    Garde seulement le dernier rapport par asset (USDJPY, EURUSD, GBPUSD).

    Affiche:
    ═══════════════════════════════════════════════════════════════════════════════
    📊 SCALPING MULTI-ACTIFS - 31 Dec 2025 14:30:05
    ───────────────────────────────────────────────────────────────────────────────
    USDJPY  │ TREND(0.8)   │ 🟢 85/BUY  │ ✅ GO   │ 📈 BUY   │ 75%  │ 137 ticks
    EURUSD  │ RANGE(0.6)   │ 🟡 45/SEL  │ ❌ VETO │ ⏸️ HOLD  │ 30%  │ 148 ticks
    GBPUSD  │ CONS(0.7)    │ 🔴 15/NEU  │ ❌ VETO │ ⏸️ HOLD  │ 10%  │ 162 ticks
    ───────────────────────────────────────────────────────────────────────────────
    📈 Signaux: 1 BUY | ⚠️ Veto: EURUSD, GBPUSD (Heure non autorisée)
    ═══════════════════════════════════════════════════════════════════════════════
    """
    display_interval = 5.0  # 5 secondes entre chaque affichage

    logger.info("📊 [DASHBOARD] Thread démarré (affichage 5s)")

    while not stop_event.is_set():
        try:
            # (05 JAN 2026): VIDER la queue complètement pour éviter accumulation
            # Problème: assets produisent 6 rapports/5s, dashboard ne consomme que 3
            # Solution: drainer toute la queue et garder seulement le dernier par asset
            reports = {}

            # Vider la queue complètement (non-bloquant)
            while True:
                try:
                    report = display_queue.get_nowait()
                    reports[report["asset"]] = report  # Écrase ancien rapport du même asset
                    display_queue.task_done()
                except queue.Empty:
                    break  # Queue vidée

            # Afficher seulement si on a au moins 1 rapport
            if reports:
                # Header
                now = pd.Timestamp.now().strftime("%d %b %Y %H:%M:%S")
                print("\n" + "═" * 110)
                print(f"📊 SCALPING MULTI-ACTIFS - {now}")
                print("─" * 110)

                # Table header (08 JAN 2026: Ajout TREND + DELTA)
                print(f"{'ASSET':<7} │ {'RÉGIME':<12} │ {'TREND':<8} │ {'SCORING':<11} │ {'DELTA':<7} │ {'TIMING':<7} │ {'ACTION':<8} │ {'CONF':<4} │ {'TICKS':<10}")
                print("─" * 110)

                # Lignes par asset (ordre fixe)
                for asset_name in ["USDJPY", "NAS100", "GBPUSD"]:
                    if asset_name in reports:
                        r = reports[asset_name]

                        # Format régime
                        regime_short = r["regime"][:4] if r["regime"] else "UNKN"
                        regime_str = f"{regime_short}({r['regime_strength']:.1f})"

                        # Icône + score (03 JAN 2026: .1f pour afficher composite avec décimale)
                        of_score = r["of_score"]
                        if of_score >= 70:
                            of_icon = "🟢"
                        elif of_score >= 40:
                            of_icon = "🟡"
                        else:
                            of_icon = "🔴"
                        bias_short = r["of_bias"][:3]
                        of_str = f"{of_icon} {of_score:.1f}/{bias_short}"

                        # Icône timing
                        timing = r["timing"]
                        if timing == "PASS":
                            timing_str = "✅ GO"
                        elif timing == "VETO":
                            timing_str = "❌ VETO"
                        else:
                            timing_str = "⏸️ HOLD"

                        # Icône action
                        action = r["action"]
                        if action == "BUY":
                            action_str = "📈 BUY"
                        elif action == "SELL":
                            action_str = "📉 SELL"
                        else:
                            action_str = "⏸️ HOLD"

                        # Confidence
                        conf_str = f"{r['confidence']*100:.0f}%"

                        # Trend (08 JAN 2026)
                        trend_str = r.get("trend", "⚪ NEU")

                        # Delta (08 JAN 2026)
                        delta_val = r.get("delta", 0)
                        if delta_val > 0:
                            delta_str = f"🟢{delta_val:+.0f}"
                        elif delta_val < 0:
                            delta_str = f"🔴{delta_val:+.0f}"
                        else:
                            delta_str = "⚪0"

                        # Ticks
                        ticks_str = f"{r['tick_count']} ticks"

                        # Affichage ligne principale (08 JAN 2026: Ajout TREND + DELTA)
                        print(f"{asset_name:<7} │ {regime_str:<12} │ {trend_str:<8} │ {of_str:<10} │ {delta_str:<7} │ {timing_str:<7} │ {action_str:<8} │ {conf_str:<4} │ {ticks_str:<10}")

                        # MODE VERBOSE: Afficher détails techniques (05 JAN 2026)
                        if verbose:
                            # Rationale/Veto reason
                            rationale = r.get("rationale", "N/A")
                            timing_reason = r.get("timing_reason", "")

                            details_line = f"    └─ "
                            if timing == "VETO" and timing_reason:
                                # Extraire les infos du veto
                                details_line += f"🚫 {timing_reason[:70]}"
                            else:
                                details_line += f"💡 {rationale}"

                            print(details_line)

                # Footer avec résumé
                print("─" * 110)

                # Compter signaux
                buy_count = sum(1 for r in reports.values() if r["action"] == "BUY")
                sell_count = sum(1 for r in reports.values() if r["action"] == "SELL")

                # Lister vetos
                veto_assets = [asset for asset, r in reports.items() if r["timing"] == "VETO"]
                veto_reason = ""
                if veto_assets:
                    # Prendre la raison du premier veto
                    first_veto = reports[veto_assets[0]]
                    reason = first_veto.get("timing_reason", "")
                    if reason:
                        # Extraire juste "Heure Xh GMT NON autorisée"
                        if "Heure" in reason and "GMT" in reason:
                            veto_reason = f" ({reason.split('(')[0].strip()})"
                        else:
                            veto_reason = f" ({reason[:40]}...)" if len(reason) > 40 else f" ({reason})"

                # Résumé
                summary_parts = []
                if buy_count > 0:
                    summary_parts.append(f"📈 {buy_count} BUY")
                if sell_count > 0:
                    summary_parts.append(f"📉 {sell_count} SELL")
                if veto_assets:
                    summary_parts.append(f"⚠️ Veto: {', '.join(veto_assets)}{veto_reason}")

                if summary_parts:
                    print(" | ".join(summary_parts))

                print("═" * 110)

            # Attendre jusqu'au prochain affichage
            time.sleep(display_interval)

        except Exception as e:
            logger.error(f"[DASHBOARD] Erreur affichage: {e}", exc_info=True)
            time.sleep(display_interval)

    logger.info("🛑 [DASHBOARD] Thread arrêté")
