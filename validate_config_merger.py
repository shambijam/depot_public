#!/usr/bin/env python3
"""
Script de validation Phase 2 - Verifie que ConfigMerger produit la bonne structure.

Checks pour chaque asset (NAS100, GBPUSD, USDJPY, EURUSD):
1. closure_rules est a entry_rules.scalping.burst_scalping.closure_rules (PAS a la racine)
2. closure_rules.target_profit_pips correspond a la valeur du JSON asset
3. closure_rules.max_loss_pips est correct
4. sltp est a entry_rules.scalping.burst_scalping.sltp avec les bons pips
5. footprint, orderflow_v6, timing_gatekeeper sont presents dans burst_scalping
6. closure_rules n'est PAS a la racine merged["closure_rules"]
7. symbol_info est injecte
"""

import sys
import os
import logging

# Setup logging
logging.basicConfig(level=logging.WARNING, format="%(levelname)s: %(message)s")

# Add project root to path
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from core.config_loader import ConfigLoader
from core.config_manager import ConfigManager
from core.config_merge import ConfigMerger


def run_checks():
    """Run all validation checks."""
    print("=" * 70)
    print("  VALIDATION ConfigMerger - Phase 2")
    print("=" * 70)

    # Init
    cm = ConfigManager()
    merger = cm.config_merger

    # Expected values per asset
    expected = {
        "NAS100": {
            "target_profit_pips": 170,
            "max_loss_pips": 2500,
            "sl_pips": 800,
            "tp_pips": 1200,
        },
        "GBPUSD": {
            "target_profit_pips": 1.6,
            "max_loss_pips": 25.0,
            "sl_pips": 20.0,
            "tp_pips": 30.0,
        },
        "USDJPY": {
            "target_profit_pips": 1.23,
            "max_loss_pips": 15.0,
            "sl_pips": 2.5,
            "tp_pips": 3.8,
        },
        "EURUSD": {
            "target_profit_pips": 1.6,
            "max_loss_pips": 20.0,
            "sl_pips": 15.0,
            "tp_pips": 23.0,
        },
    }

    all_pass = True
    total_checks = 0
    passed_checks = 0

    for asset, exp in expected.items():
        print(f"\n--- {asset} ---")
        merged = merger.get_merged_config(asset, "scalping", force_reload=True)

        if not merged:
            print(f"  FAIL: get_merged_config returned empty for {asset}")
            all_pass = False
            total_checks += 1
            continue

        burst = (
            merged.get("entry_rules", {})
            .get("scalping", {})
            .get("burst_scalping", {})
        )

        # Check 1: closure_rules in burst_scalping
        total_checks += 1
        cr = burst.get("closure_rules", {})
        if cr:
            print(f"  PASS [1] closure_rules present in burst_scalping")
            passed_checks += 1
        else:
            print(f"  FAIL [1] closure_rules MISSING from burst_scalping")
            all_pass = False

        # Check 2: target_profit_pips
        total_checks += 1
        actual_tp = cr.get("target_profit_pips")
        if actual_tp == exp["target_profit_pips"]:
            print(f"  PASS [2] target_profit_pips={actual_tp} (expected {exp['target_profit_pips']})")
            passed_checks += 1
        else:
            print(f"  FAIL [2] target_profit_pips={actual_tp} (expected {exp['target_profit_pips']})")
            all_pass = False

        # Check 3: max_loss_pips
        total_checks += 1
        actual_ml = cr.get("max_loss_pips")
        if actual_ml == exp["max_loss_pips"]:
            print(f"  PASS [3] max_loss_pips={actual_ml} (expected {exp['max_loss_pips']})")
            passed_checks += 1
        else:
            print(f"  FAIL [3] max_loss_pips={actual_ml} (expected {exp['max_loss_pips']})")
            all_pass = False

        # Check 4: sltp in burst_scalping with correct values
        total_checks += 1
        sltp = burst.get("sltp", {})
        sl_pips = sltp.get("sl", {}).get("pips") if isinstance(sltp.get("sl"), dict) else None
        tp_pips = sltp.get("tp", {}).get("pips") if isinstance(sltp.get("tp"), dict) else None
        if sl_pips == exp["sl_pips"] and tp_pips == exp["tp_pips"]:
            print(f"  PASS [4] sltp: sl={sl_pips}, tp={tp_pips}")
            passed_checks += 1
        else:
            print(f"  FAIL [4] sltp: sl={sl_pips} (exp {exp['sl_pips']}), tp={tp_pips} (exp {exp['tp_pips']})")
            all_pass = False

        # Check 5: footprint, orderflow_v6, timing_gatekeeper in burst_scalping
        total_checks += 1
        sections_present = []
        sections_missing = []
        for section in ["footprint", "orderflow_v6", "timing_gatekeeper"]:
            if section in burst:
                sections_present.append(section)
            else:
                sections_missing.append(section)
        if not sections_missing:
            print(f"  PASS [5] All sections present in burst_scalping: {sections_present}")
            passed_checks += 1
        else:
            print(f"  FAIL [5] Missing from burst_scalping: {sections_missing}")
            all_pass = False

        # Check 6: closure_rules NOT at root
        total_checks += 1
        if "closure_rules" not in merged:
            print(f"  PASS [6] closure_rules NOT at root level")
            passed_checks += 1
        else:
            print(f"  FAIL [6] closure_rules found at ROOT level (should only be in burst_scalping)")
            all_pass = False

        # Check 7: symbol_info injected
        total_checks += 1
        si = merged.get("symbol_info", {})
        if si and "point" in si and "digits" in si:
            print(f"  PASS [7] symbol_info injected (point={si['point']}, digits={si['digits']})")
            passed_checks += 1
        else:
            print(f"  FAIL [7] symbol_info missing or incomplete: {si}")
            all_pass = False

    print(f"\n{'=' * 70}")
    print(f"  RESULTS: {passed_checks}/{total_checks} checks passed")
    if all_pass:
        print(f"  ALL PASS - Safe to proceed to Phase 3")
    else:
        print(f"  SOME CHECKS FAILED - DO NOT proceed to Phase 3")
    print(f"{'=' * 70}")

    return all_pass


if __name__ == "__main__":
    success = run_checks()
    sys.exit(0 if success else 1)
