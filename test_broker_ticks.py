#!/usr/bin/env python3
"""
🔍 TEST CRITIQUE : Vérifier si le broker fournit les données tick historiques

Ce script teste directement la capacité du broker à fournir des ticks
pour une bougie M1 fermée récente (il y a 5 minutes).
"""

import sys
import MetaTrader5 as mt5
from datetime import datetime, timedelta, timezone
import pandas as pd


def test_broker_ticks():
    """Test la disponibilité des ticks historiques depuis le broker"""

    print("=" * 80)
    print("🧪 TEST DISPONIBILITÉ TICKS BROKER MT5")
    print("=" * 80)
    print()

    # === INITIALISATION MT5 ===
    print("1️⃣ Initialisation MT5...")
    if not mt5.initialize():
        print(f"   ❌ ERREUR: Impossible d'initialiser MT5")
        print(f"   Error code: {mt5.last_error()}")
        return False

    print(f"   ✅ MT5 initialisé")
    print(f"   Version: {mt5.version()}")
    print()

    # === VÉRIFICATION COMPTE ===
    account_info = mt5.account_info()
    if account_info is None:
        print(f"   ❌ ERREUR: Aucun compte connecté")
        mt5.shutdown()
        return False

    print(f"   ✅ Compte connecté: {account_info.login}")
    print(f"   Serveur: {account_info.server}")
    print()

    # === VÉRIFICATION SYMBOLE ===
    symbol = "XAUUSD"
    print(f"2️⃣ Vérification symbole {symbol}...")

    symbol_info = mt5.symbol_info(symbol)
    if symbol_info is None:
        print(f"   ❌ ERREUR: Symbole {symbol} non trouvé")
        mt5.shutdown()
        return False

    print(f"   ✅ Symbole trouvé: {symbol}")
    print(f"   Visible: {symbol_info.visible}")

    # Activer le symbole si nécessaire
    if not symbol_info.visible:
        print(f"   ⚠️ Symbole non visible, activation...")
        if not mt5.symbol_select(symbol, True):
            print(f"   ❌ ERREUR: Impossible d'activer le symbole")
            mt5.shutdown()
            return False
        print(f"   ✅ Symbole activé")

    print()

    # === TEST TICKS RÉCENTS (dernières 30 secondes) ===
    print("3️⃣ Test ticks LIVE (dernières 30 secondes)...")

    now = datetime.now(timezone.utc)
    live_start = now - timedelta(seconds=30)

    print(f"   Période: {live_start.strftime('%H:%M:%S')} → {now.strftime('%H:%M:%S')} UTC")

    live_ticks = mt5.copy_ticks_range(symbol, live_start, now, mt5.COPY_TICKS_ALL)

    if live_ticks is None or len(live_ticks) == 0:
        print(f"   ❌ AUCUN tick reçu (dernières 30s)")
        print(f"   ⚠️ Marché fermé ou broker ne diffuse pas les ticks en temps réel")
    else:
        print(f"   ✅ {len(live_ticks)} ticks reçus (dernières 30s)")
        print(f"   → Tick/s: {len(live_ticks) / 30:.2f}")

        # Afficher quelques ticks
        df_live = pd.DataFrame(live_ticks)
        if len(df_live) > 0:
            print()
            print("   📊 Échantillon (5 premiers ticks):")
            print(f"   {'Time':<20} {'Bid':<12} {'Ask':<12} {'Flags':<8}")
            print("   " + "-" * 60)
            for i, row in df_live.head(5).iterrows():
                ts = pd.to_datetime(row.get('time', 0), unit='s', utc=True)
                bid = row.get('bid', 0)
                ask = row.get('ask', 0)
                flags = row.get('flags', 0)
                print(f"   {ts.strftime('%H:%M:%S.%f')[:-3]:<20} {bid:<12.5f} {ask:<12.5f} {flags:<8}")

    print()

    # === TEST TICKS HISTORIQUES (bougie fermée il y a 5 minutes) ===
    print("4️⃣ Test ticks HISTORIQUES (bougie M1 fermée il y a 5 minutes)...")

    # Calculer timestamp bougie fermée (il y a 5 min, aligné sur minute)
    minutes_ago = 5
    now = datetime.now(timezone.utc)
    hist_end = (now - timedelta(minutes=minutes_ago)).replace(second=0, microsecond=0)
    hist_start = hist_end - timedelta(minutes=1)

    print(f"   Bougie M1: {hist_start.strftime('%Y-%m-%d %H:%M:%S')} → {hist_end.strftime('%H:%M:%S')} UTC")
    print(f"   (il y a {minutes_ago} minutes)")

    hist_ticks = mt5.copy_ticks_range(symbol, hist_start, hist_end, mt5.COPY_TICKS_ALL)

    if hist_ticks is None or len(hist_ticks) == 0:
        print(f"   ❌ AUCUN tick historique reçu")
        print()
        print("   🔍 DIAGNOSTIC:")
        print("   • Le broker ne stocke PAS les ticks historiques")
        print("   • Seules les bougies OHLC sont disponibles")
        print("   • Le Footprint ne peut PAS fonctionner avec ce broker")
        print()
        result = False
    else:
        print(f"   ✅ {len(hist_ticks)} ticks historiques reçus")
        print(f"   → Tick/s: {len(hist_ticks) / 60:.2f}")

        # Analyse détaillée
        df_hist = pd.DataFrame(hist_ticks)
        if len(df_hist) > 0:
            df_hist['time_dt'] = pd.to_datetime(df_hist['time'], unit='s', utc=True)

            # Vérifier la fenêtre temporelle stricte
            ticks_in_window = df_hist[
                (df_hist['time_dt'] >= hist_start) &
                (df_hist['time_dt'] < hist_end)
            ]

            print()
            print(f"   📊 Analyse détaillée:")
            print(f"   • Ticks bruts reçus:     {len(df_hist)}")
            print(f"   • Ticks dans fenêtre:    {len(ticks_in_window)}")
            print(f"   • Couverture temporelle: {(ticks_in_window['time_dt'].max() - ticks_in_window['time_dt'].min()).total_seconds():.1f}s")

            if len(ticks_in_window) >= 3:
                print()
                print("   ✅ SUFFISANT pour Footprint (≥3 ticks requis)")
                result = True
            else:
                print()
                print(f"   ⚠️ INSUFFISANT pour Footprint (besoin ≥3 ticks, reçu {len(ticks_in_window)})")
                result = False

            # Échantillon
            if len(ticks_in_window) > 0:
                print()
                print("   📊 Échantillon (5 premiers ticks dans fenêtre):")
                print(f"   {'Time':<20} {'Bid':<12} {'Ask':<12} {'Flags':<8}")
                print("   " + "-" * 60)
                for i, row in ticks_in_window.head(5).iterrows():
                    ts = row['time_dt']
                    bid = row.get('bid', 0)
                    ask = row.get('ask', 0)
                    flags = row.get('flags', 0)
                    print(f"   {ts.strftime('%H:%M:%S.%f')[:-3]:<20} {bid:<12.5f} {ask:<12.5f} {flags:<8}")
        else:
            result = False

    print()

    # === TEST TICKS TRÈS RÉCENTS (bougie fermée il y a 2 minutes) ===
    print("5️⃣ Test ticks TRÈS RÉCENTS (bougie M1 fermée il y a 2 minutes)...")

    minutes_ago = 2
    recent_end = (now - timedelta(minutes=minutes_ago)).replace(second=0, microsecond=0)
    recent_start = recent_end - timedelta(minutes=1)

    print(f"   Bougie M1: {recent_start.strftime('%H:%M:%S')} → {recent_end.strftime('%H:%M:%S')} UTC")
    print(f"   (il y a {minutes_ago} minutes)")

    recent_ticks = mt5.copy_ticks_range(symbol, recent_start, recent_end, mt5.COPY_TICKS_ALL)

    if recent_ticks is None or len(recent_ticks) == 0:
        print(f"   ❌ AUCUN tick reçu")
    else:
        print(f"   ✅ {len(recent_ticks)} ticks reçus")

        df_recent = pd.DataFrame(recent_ticks)
        if len(df_recent) > 0:
            df_recent['time_dt'] = pd.to_datetime(df_recent['time'], unit='s', utc=True)
            ticks_in_window = df_recent[
                (df_recent['time_dt'] >= recent_start) &
                (df_recent['time_dt'] < recent_end)
            ]
            print(f"   • Ticks dans fenêtre: {len(ticks_in_window)}")

            if len(ticks_in_window) >= 3:
                print(f"   ✅ SUFFISANT pour Footprint")
            else:
                print(f"   ⚠️ INSUFFISANT pour Footprint ({len(ticks_in_window)}/3)")

    print()

    # === SHUTDOWN ===
    mt5.shutdown()

    # === RÉSUMÉ ===
    print("=" * 80)
    print("📊 RÉSUMÉ")
    print("=" * 80)
    print()

    if live_ticks and len(live_ticks) > 0:
        print(f"✅ Ticks LIVE (temps réel):      {len(live_ticks)} ticks/30s")
    else:
        print(f"❌ Ticks LIVE (temps réel):      AUCUN")

    if hist_ticks and len(hist_ticks) > 0:
        print(f"✅ Ticks HISTORIQUES (5 min):    {len(hist_ticks)} ticks")
    else:
        print(f"❌ Ticks HISTORIQUES (5 min):    AUCUN")

    if recent_ticks and len(recent_ticks) > 0:
        print(f"✅ Ticks TRÈS RÉCENTS (2 min):   {len(recent_ticks)} ticks")
    else:
        print(f"❌ Ticks TRÈS RÉCENTS (2 min):   AUCUN")

    print()

    if result:
        print("🎉 CONCLUSION: Broker fournit les ticks historiques !")
        print("   → Le Footprint DEVRAIT fonctionner")
        print("   → Chercher le problème ailleurs (timestamps, config, code)")
    else:
        print("⚠️ CONCLUSION: Problème détecté avec les ticks")
        print("   → Vérifier si marché ouvert au moment du test")
        print("   → Vérifier config broker / abonnement données tick")
        print("   → Sinon, implémenter simulation Footprint depuis OHLC")

    print()
    print("=" * 80)

    return result


if __name__ == "__main__":
    try:
        success = test_broker_ticks()
        sys.exit(0 if success else 1)
    except Exception as e:
        print(f"\n❌ ERREUR FATALE: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)
