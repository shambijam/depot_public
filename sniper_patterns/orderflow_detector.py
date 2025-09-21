# sniper_patterns/orderflow_detector.py
"""
OrderFlowDetector v2 (Desk Quant)
--------------------------------------------------
Lecture avancée du flux d’ordres :
- Volume Delta & CVD
- Footprint intra-bar (agression ask/bid)
- Imbalances (buy/sell dominance, absorption)
- Détection iceberg (volumes cachés suspects)

Retourne un tableau brut de signaux factuels.
"""

import pandas as pd
from typing import List, Dict, Any, Optional


def detect_orderflow(df: pd.DataFrame) -> List[Optional[Dict[str, Any]]]:
    if df is None or len(df) < 1:
        return []

    signals: List[Optional[Dict[str, Any]]] = []

    for i in range(len(df)):
        try:
            sig: Dict[str, Any] = {}
            ts = str(df.index[i]) if hasattr(df.index, "dtype") else None

            bid_vol = df["bid_volume"].iloc[i] if "bid_volume" in df.columns else None
            ask_vol = df["ask_volume"].iloc[i] if "ask_volume" in df.columns else None
            total = (bid_vol or 0) + (ask_vol or 0)

            delta = (ask_vol - bid_vol) if (bid_vol is not None and ask_vol is not None) else None
            imbalance = (ask_vol / total) if total > 0 else None
            dominance = "buyers" if delta and delta > 0 else "sellers" if delta and delta < 0 else "neutral"

            pattern = None
            extra = {}

            # === 1️⃣ Delta / Imbalance bruts ===
            if imbalance is not None:
                if imbalance > 0.7:
                    pattern = "buy_imbalance"
                elif imbalance < 0.3:
                    pattern = "sell_imbalance"

            if bid_vol and ask_vol:
                if bid_vol > 2 * ask_vol:
                    pattern = "sell_absorption"
                elif ask_vol > 2 * bid_vol:
                    pattern = "buy_absorption"

            # === 2️⃣ CVD (Cumulative Volume Delta) ===
            if "cvd" in df.columns:
                sig["cvd"] = float(df["cvd"].iloc[i])

            # === 3️⃣ Footprint intra-bar (si dispo) ===
            if "aggressor_buy_vol" in df.columns and "aggressor_sell_vol" in df.columns:
                buy_aggr = df["aggressor_buy_vol"].iloc[i]
                sell_aggr = df["aggressor_sell_vol"].iloc[i]

                if buy_aggr > 2 * sell_aggr:
                    pattern = "aggressive_buying"
                    extra["footprint"] = f"buy>{buy_aggr},sell>{sell_aggr}"
                elif sell_aggr > 2 * buy_aggr:
                    pattern = "aggressive_selling"
                    extra["footprint"] = f"buy>{buy_aggr},sell>{sell_aggr}"

            # === 4️⃣ Détection Iceberg ===
            # Hypothèse : exécution répétée à même prix avec volumes anormalement stables
            if "executions_count" in df.columns and "avg_exec_size" in df.columns:
                exec_count = df["executions_count"].iloc[i]
                avg_size = df["avg_exec_size"].iloc[i]

                if exec_count > 50 and avg_size < 0.2 * (total or 1):
                    pattern = "iceberg_order"
                    extra["iceberg"] = {"exec_count": int(exec_count), "avg_size": float(avg_size)}

            # === Assemblage final ===
            if pattern:
                sig.update(
                    {
                        "index": i,
                        "timestamp": ts,
                        "orderflow_pattern": pattern,
                        "imbalance_pct": round(imbalance, 3) if imbalance is not None else None,
                        "dominance": dominance,
                        "delta": delta,
                        "bid_volume": bid_vol,
                        "ask_volume": ask_vol,
                    }
                )
                sig.update(extra)
                signals.append(sig)
            else:
                signals.append(None)

        except Exception as e:
            print(f"Erreur orderflow_detector à l’index {i}: {e}")
            signals.append(None)

    return signals
